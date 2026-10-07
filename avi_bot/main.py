"""Assemble the robot. BUILD_SPEC 13.

Everything until now has been parts that can be tested alone. This is the one
file whose job is wiring, and it is deliberately the only place that knows how
the parts fit together -- so a test can build any single piece without
dragging in a serial port, and this file can be read to find out what talks to
what.

WHAT THIS FILE IS NOT
It is not a loop that does everything. The motion engine already owns its own
50 Hz thread and the detector its own few-hertz thread, because they fail
differently and must not block each other -- a GPU stall must never delay a P
frame inside the firmware's 500 ms watchdog. `Avi.handle()` and `Avi.tick()`
are called BY those existing loops rather than replacing them.

THE ORDER OF OPERATIONS IN handle() IS THE POINT
Hear, route, execute, then speak -- and the speech policy is applied once at
the end, to every route. A tool that produced a sentence cannot bypass the
policy by being reached through the local rules instead of the model.
"""
import time
from datetime import datetime

from . import env as env_mod                                   # noqa: F401
from . import numbers, scheduler as sched_mod                   # noqa: F401
from .brain import context as context_mod
from .brain import providers, router as router_mod, speech, tools
from .memory import db as memdb
from .motion import poses as poses_mod
from .skills import (apps as apps_mod, calendar as cal_mod,
                     find as find_mod, focus as focus_mod,
                     lamp as lamp_mod, media as media_mod,
                     memory_tools, modes as modes_mod, registry,
                     reminders as rem_mod, status as status_mod,
                     timers as timers_mod, weather as weather_mod)


from avi_bot.motion import backlash as backlash_mod             # noqa: E402


class Avi:
    """The whole robot, minus the threads that already exist."""

    def __init__(self, db_path=":memory:", clock=time.time,
                 arm=None, mood=None, ics_text=None, calendar_url=None,
                 lamp_host=None, apps_path=None, env=None,
                 send=None, local_pick=None, observed=None, situation=None):
        self.clock = clock
        self.started_at = clock()
        # Sentences AVI noticed by watching (presence, desk layout); see
        # perception/observations.py. None when headless.
        self.observed = observed
        # () -> {"activity": str, "objects": [label, ...]}: what the cloud
        # may be told about right now, as text (brain/context.py ALLOWED).
        # Without it every cloud turn was answered blind -- not even the time
        # of day reached the model.
        self.situation = situation
        # `arm` is the dashboard's Arm, or None in a headless test. Every use
        # of it is guarded, so the brain runs with no hardware at all.
        self.arm = arm
        self.mood = mood
        self._sleep = time.sleep          # tests replace it; motion waits

        self.store = memdb.Store(db_path, clock=clock)
        self.policy = speech.SpeechPolicy()
        # Before the chain is built, or it reads an environment that has no
        # keys in it and reports "groq: no key" -- which looks exactly like a
        # rate limit and sends every turn to the local fallback.
        if env is None:
            self.loaded_keys = env_mod.load()
        else:
            self.loaded_keys = []
        self.budget = providers.Budget()
        self.chain = providers.Chain(budget=self.budget, send=send, env=env)
        self.router = router_mod.Router(chain=self.chain, policy=self.policy,
                                        clock=clock, local_pick=local_pick)

        self.timers = timers_mod.Timers(store=self.store, clock=clock)
        self.focus = focus_mod.Focus(clock=clock)
        self.modes = modes_mod.Modes(clock=clock, policy=self.policy,
                                     on_pose=self._pose,
                                     on_logging=self._set_logging)
        # clock=clock is not cosmetic. Without it the calendar read real time
        # while every other part of Avi read the injected clock, so one object
        # ran on two timelines: a pinned test at 2025-10-09 would get calendar
        # warnings for the real 2026-10-06, and test_a_due_timer_surfaces
        # failed only during the ten minutes before the fixture's 10:00
        # Chemistry -- a one-minute window, once a day.
        self.calendar = cal_mod.Calendar(ics_text=ics_text, url=calendar_url,
                                         clock=clock)
        self.lamp = lamp_mod.Lamp(host=lamp_host)
        self.media = media_mod.Media()
        self.apps = (apps_mod.Apps(path=apps_path) if apps_path
                     else apps_mod.Apps())
        self.scheduler = sched_mod.Scheduler(
            timers=self.timers, store=self.store, calendar=self.calendar,
            modes=self.modes, clock=clock)

        self.skills = registry.Registry()
        try:
            self._wire()
        except Exception:
            # The stores are open by now; a failed build must not leak them.
            self.close()
            raise
        self.logging_enabled = True
        self.turns = []

    # --- wiring ----------------------------------------------------------
    def _wire(self):
        handlers = {
            "set_timer": lambda seconds, label=None: self.timers.set(
                seconds, label) and self._timer_set(seconds),
            "cancel_timer": lambda id=None: self._cancelled(
                self.timers.cancel(id)),
            "list_timers": lambda: self.timers.describe(),
            "get_time": self._time,
            "get_schedule": lambda day="today": self.calendar.describe(day),
            "weather": lambda day="today": weather_mod.fetch(day),
            "find_object": self._find,
            "open_app": lambda name: self.apps.open(name),
            "robot_status": None,        # filled below, needs the budget
            "praise": lambda: self._mood_event("praise"),
            "scold": lambda: self._mood_event("scold"),
            "stop": self._stop,
            "look_at": self._look_at,
            "play_emotion": self._play,
        }
        handlers.update(memory_tools.make(self.store,
                                          observed=self.observed))
        handlers.update(focus_mod.make(self.focus))
        handlers.update(modes_mod.make(self.modes))
        handlers.update(lamp_mod.make(self.lamp))
        handlers.update(media_mod.make(self.media))
        handlers.update(rem_mod.make(self.store))
        handlers.update(status_mod.make(mood=self.mood, budget=self.budget,
                                       started_at=self.started_at,
                                       clock=self.clock))
        for name, fn in handlers.items():
            if fn is not None:
                self.skills.register(name, fn)
        missing = self.skills.missing()
        if missing:
            # Loud, because a tool in the schema with nothing behind it is a
            # promise the robot cannot keep, and the model has been told it can.
            raise RuntimeError(f"tools with no handler: {missing}")

    # --- small handlers --------------------------------------------------
    def _timer_set(self, seconds):
        return (f"{self.timers._spoken(int(seconds))}s." if seconds < 60
                else None), {"seconds": int(seconds)}

    @staticmethod
    def _cancelled(n):
        return (None if n else "No timers."), {"cancelled": n}

    def _time(self):
        lt = time.localtime(self.clock())
        hour = lt.tm_hour % 12 or 12
        return (f"{hour}" if lt.tm_min == 0
                else f"{hour}:{lt.tm_min:02d}") + ".", None

    def _find(self, name):
        """Answer where something is, and look at it when that is knowable.

        The centre angles are NOT optional and NOT defaulted. The logger
        records object positions only from one pose, so a stored image position
        means nothing without that pose's own angles -- and the pose has both
        moved (OBSERVE from base_yaw 90 to 60) and been replaced (OBSERVE to
        ATTENTIVE, because OBSERVE turned out to face a blank wall). Either
        change would have silently invalidated a hard-coded default.
        """
        if self.arm is not None and hasattr(self.arm, "find_object"):
            # Hosted in the dashboard (dashboard/brain_host.py): its find is
            # the one with the room map in world geometry, the backlash-safe
            # turn and the desk layout, so there is one way to find a thing.
            got = self.arm.find_object(name, look=True) or {}
            aim = got.get("aim") or {}
            return got.get("say"), {"moved": bool(got.get("moved")),
                                    "source": aim.get("source"),
                                    "confident": bool(aim.get("confident"))}
        if self.arm is None or not hasattr(self.arm, "db"):
            return "I can't see the desk right now.", None
        cam = self.arm.config.camera
        observe = poses_mod.POSES.get(poses_mod.OBSERVE_POSE) or {}
        finder = find_mod.Finder(
            self.arm.db, cam["yaw_deg_per_frame"],
            wrist_deg_per_frame=cam.get("wrist_deg_per_frame", 32.2),
            centre_yaw=observe.get("base_yaw"),
            centre_wrist=observe.get("wrist"),
            roommap=getattr(self.arm, "roommap", None))
        text, yaw, found = finder.find(name)
        aim = finder.aim(name)
        # Only a direction the finder vouches for drives the arm. Pointing
        # confidently at the wrong wall is worse than not turning.
        if aim and aim.get("confident"):
            # Shape FIRST. A map direction is only true at the shoulder and
            # elbow it was surveyed at, so turning yaw and wrist before the
            # arm is in that shape points it somewhere else.
            for joint in ("shoulder", "elbow"):
                if aim.get(joint) is not None:
                    self._safe(lambda j=joint: self.arm.set(j, aim[j]))
            self._safe(lambda: self._yaw_to(aim["base_yaw"]))
            if aim.get("wrist") is not None:
                self._safe(lambda: self.arm.set("wrist", aim["wrist"]))
        return text, {"yaw": yaw, "found": found,
                      "source": (aim or {}).get("source"),
                      "confident": bool(aim and aim.get("confident"))}

    def _pose(self, name):
        self._safe(lambda: self.arm.recall(name))

    def _set_logging(self, enabled):
        self.logging_enabled = bool(enabled)

    def _mood_event(self, name):
        if self.mood is not None:
            self._safe(lambda: self.mood.event(name))
        return None, {"event": name}

    def _stop(self):
        self._safe(lambda: self.arm.cancel())
        return None, {"stopped": True}

    def _look_at(self, target):
        self._safe(lambda: self.arm.look_at(target))
        return None, {"target": target}

    def _play(self, name):
        self._safe(lambda: self.arm.play(name))
        return None, {"gesture": name}

    def _yaw_to(self, target):
        """base_yaw to `target`, last move upward: it has 12-24 degrees of
        backlash, so a target reached going down is off by up to that."""
        lo, _ = self.arm.config.soft_limits("base_yaw")
        here = self.arm.snapshot().get("base_yaw")
        cmds = backlash_mod.plan(here, target, lo)
        for i, cmd in enumerate(cmds):
            self.arm.set("base_yaw", cmd)
            if i < len(cmds) - 1:
                self._sleep(backlash_mod.travel_s(here, cmd))
                here = cmd

    def _safe(self, fn):
        """Hardware calls never take the brain down. The arm has its own
        watchdog; a failed gesture must not cost him a timer."""
        if self.arm is None:
            return None
        try:
            return fn()
        except Exception:
            return None

    # --- the two entry points --------------------------------------------
    def handle(self, utterance):
        """One turn: hear, route, execute, speak. Never raises."""
        sit = {}
        if self.situation is not None:
            try:
                sit = self.situation() or {}
            except Exception:
                sit = {}              # a turn is never lost to its context
        ctx = context_mod.from_store(
            self.store, utterance=utterance,
            now=datetime.fromtimestamp(self.clock()),
            activity=sit.get("activity"), objects=sit.get("objects"),
            mood=(self.mood.emotion() if self.mood else None),
            turns=self.turns[-6:])
        decision = self.router.handle(utterance, context=ctx)
        results = self.skills.run_all(decision.calls)

        # A skill that produced something sayable overrides the model's own
        # words, because the skill actually knows the answer and the model was
        # guessing at it.
        said = next((r.text for r in results if r.ok and r.text), None)
        if said:
            decision.reply = self.policy.apply(
                decision.reply.model_copy(update={"say": said}),
                decision.reason)

        if self.logging_enabled:
            self.store.log_interaction(
                utterance, decision.route, provider=decision.provider,
                tools=[c[0] for c in decision.calls],
                latency_ms=decision.latency_ms,
                outcome="ok" if all(r.ok for r in results) else "failed")
        self.turns.append({"who": "him", "text": utterance})
        if decision.reply.say:
            self.turns.append({"who": "avi", "text": decision.reply.say})
        return decision, results

    # --- the first sit-down of the day ---------------------------------
    RUNDOWN_ITEMS = 3

    def rundown(self, now=None):
        """What is still ahead today, for his first sit-down. BUILD_SPEC 9.

        Reminders due later today, and calendar events if a calendar is
        configured. At most once a day (days start at 04:00, see
        perception/habits.py). None when nothing is ahead: "nothing on
        today" every morning is the kind of talk that gets a robot switched
        off, and with no calendar configured it would be a guess.
        """
        from avi_bot.perception import habits
        now = self.clock() if now is None else float(now)
        day = habits.day_of(now)
        if getattr(self, "_rundown_day", None) == day:
            return None
        self._rundown_day = day
        end = habits.day_start_ts(now) + 86400.0
        items = []                                     # (when_ts, words)
        for r in self.store.reminders("pending"):
            if r.get("kind") == "timer" or not now < r["due_ts"] <= end:
                continue
            items.append((r["due_ts"], f"{r['text']} at "
                          f"{numbers.clock_time(r['due_ts'], am_pm=True)}"))
        if self.calendar.ics_text or self.calendar.url:
            try:
                for e in self.calendar.events("today"):
                    ts = e.start.timestamp() if hasattr(
                        e.start, "timestamp") else now
                    if e.all_day or ts > now:
                        items.append((ts if not e.all_day else now,
                                      e.summary if e.all_day else
                                      f"{e.summary} at {e.spoken_time()}"))
            except Exception:
                pass                    # an unreachable calendar is silent
        if not items:
            return None
        items.sort(key=lambda i: i[0])
        head = [w for _, w in items[:self.RUNDOWN_ITEMS]]
        more = len(items) - len(head)
        return ("Today: " + "; ".join(head)
                + (f"; and {more} more." if more else "."))

    def tick(self):
        """Called from the existing arm loop. Returns an Announcement or None."""
        return self.scheduler.next_announcement()

    def close(self):
        self.store.close()
        self.budget.close()
