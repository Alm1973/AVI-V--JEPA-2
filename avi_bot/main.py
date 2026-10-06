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

from . import env as env_mod                                   # noqa: F401
from . import numbers, scheduler as sched_mod                   # noqa: F401
from .brain import context as context_mod
from .brain import providers, router as router_mod, speech, tools
from .memory import db as memdb
from .skills import (apps as apps_mod, calendar as cal_mod,
                     find as find_mod, focus as focus_mod,
                     lamp as lamp_mod, media as media_mod,
                     memory_tools, modes as modes_mod, registry,
                     reminders as rem_mod, status as status_mod,
                     timers as timers_mod, weather as weather_mod)


class Avi:
    """The whole robot, minus the threads that already exist."""

    def __init__(self, db_path=":memory:", clock=time.time,
                 arm=None, mood=None, ics_text=None, calendar_url=None,
                 lamp_host=None, apps_path=None, env=None,
                 send=None, local_pick=None):
        self.clock = clock
        self.started_at = clock()
        # `arm` is the dashboard's Arm, or None in a headless test. Every use
        # of it is guarded, so the brain runs with no hardware at all.
        self.arm = arm
        self.mood = mood

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
        self.calendar = cal_mod.Calendar(ics_text=ics_text, url=calendar_url)
        self.lamp = lamp_mod.Lamp(host=lamp_host)
        self.media = media_mod.Media()
        self.apps = (apps_mod.Apps(path=apps_path) if apps_path
                     else apps_mod.Apps())
        self.scheduler = sched_mod.Scheduler(
            timers=self.timers, store=self.store, calendar=self.calendar,
            modes=self.modes, clock=clock)

        self.skills = registry.Registry()
        self._wire()
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
        handlers.update(memory_tools.make(self.store))
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
        if self.arm is None or not hasattr(self.arm, "db"):
            return "I can't see the desk right now.", None
        finder = find_mod.Finder(self.arm.db,
                                 self.arm.config.camera["yaw_deg_per_frame"])
        text, yaw, found = finder.find(name)
        if found and yaw is not None:
            self._safe(lambda: self.arm.set("base_yaw", yaw))
        return text, {"yaw": yaw, "found": found}

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
        ctx = context_mod.from_store(
            self.store, utterance=utterance,
            now=None, mood=(self.mood.emotion() if self.mood else None),
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

    def tick(self):
        """Called from the existing arm loop. Returns an Announcement or None."""
        return self.scheduler.next_announcement()

    def close(self):
        self.store.close()
        self.budget.close()
