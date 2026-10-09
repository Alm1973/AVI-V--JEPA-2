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
import math
import os
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


def _combined(verdicts):
    """One verdict for a move made in steps: the first step the picture
    disagreed with, else a step it confirmed, else (all blind) the last."""
    verdicts = [v for v in verdicts if v]
    if not verdicts:
        return None
    bad = [v for v in verdicts if v.get("judged") and not v.get("agrees")]
    good = [v for v in verdicts if v.get("judged") and v.get("agrees")]
    out = dict(bad[0] if bad else good[0] if good else verdicts[-1])
    out["steps_judged"] = len(bad) + len(good)
    out["steps"] = len(verdicts)
    return out


class Avi:
    """The whole robot, minus the threads that already exist."""

    def __init__(self, db_path=":memory:", clock=time.time,
                 arm=None, mood=None, ics_text=None, calendar_url=None,
                 lamp_host=None, apps_path=None, env=None,
                 send=None, local_pick=None, observed=None, situation=None,
                 cloud_first=False):
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
        # Tools that cannot work here are not offered: a lamp with no host,
        # a schedule with no calendar.
        unusable = []
        if not (ics_text or calendar_url):
            unusable.append("get_schedule")
        if not (lamp_host or os.environ.get("KASA_HOST")):
            unusable.append("lamp")
        self.router = router_mod.Router(chain=self.chain, policy=self.policy,
                                        clock=clock, local_pick=local_pick,
                                        cloud_first=cloud_first,
                                        wire_tools=tools.wire_tools(unusable))
        # Motion sequences run on their own thread so the reply is not held
        # up by a wave; tests turn this off to step through them.
        self.background = True

        self.timers = timers_mod.Timers(store=self.store, clock=clock)
        self.focus = focus_mod.Focus(clock=clock,
                                     on_change=self._mood_event)
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
            "nudge": self._nudge,
            "move_joints": self._move_joints,
            "motion_sequence": self._sequence,
            "go_to_pose": self._go_to_pose,
            "describe_view": self._describe_view,
            "look_around": self._look_around,
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
        # Not through _safe: that skips everything when there is no arm, and
        # mood is state, not hardware -- praise in a headless brain was lost.
        if self.mood is not None:
            try:
                self.mood.event(name)
            except Exception:
                pass
        return None, {"event": name}

    def _stop(self):
        self._safe(lambda: self.arm.cancel())
        return None, {"stopped": True}

    def _look_at(self, target):
        """Silent when it worked -- the turn is the answer. When the camera
        could not find what it turned to, say so: "look at the laptop" once
        ended on a mug with nothing said."""
        got = self._safe(lambda: self.arm.look_at(target))
        got = got if isinstance(got, dict) else {}
        info = {"target": target, "in_view": got.get("in_view"),
                "moved": got.get("moved")}
        if (target or "").strip().lower() in ("me", "him", "you", "person",
                                              "shaurya", "here", "over here"):
            if got.get("looked") is False:
                return "I can't see you from here.", dict(info, missed=True)
            return None, info
        if got.get("dark"):
            return (f"It's too dark for me to see the {target}.",
                    dict(info, tell=True))
        if got.get("in_view") is False:
            return (f"I can't see the {target} from here.",
                    dict(info, missed=True))
        if got.get("moved") is False and got.get("say"):
            # No aim: what it knows instead, and that it will search.
            return got["say"], dict(info, tell=True)
        return None, info

    # --- free motion: what the model improvises with ---------------------
    def _hold(self, seconds):
        """Keep the dashboard's seat loop off the arm while a move plays."""
        hold = getattr(self.arm, "hold", None)
        if callable(hold):
            self._safe(lambda: hold(seconds))

    def _judged(self, direction, do):
        """Run a move and let the picture judge it (perception/verify.py,
        via the dashboard's arm). One retry when the camera did not move at
        all -- a turn eaten by backlash or a servo that missed a frame --
        then the honest sentence if it still disagrees."""
        from avi_bot.perception import verify as verify_mod
        judge = getattr(self.arm, "judged_move", None)
        if direction is None or not callable(judge):
            self._safe(do)
            return None, None
        verdict = self._safe(lambda: judge(direction, do))
        if (verdict and verdict.get("judged") and not verdict.get("agrees")
                and set(verdict["votes"].values()) == {"none"}):
            verdict = self._safe(lambda: judge(direction, do)) or verdict
            if verdict is not None:
                verdict["retried"] = True
        return verify_mod.sentence(verdict), verdict

    # The most one judged move may be. A 30-degree pan slides the picture
    # past both judges -- ORB kept 4 inliers, JEPA's best shift sat on its
    # 9-token edge -- and the verdict came back blind (robot, 2026-10-08).
    # Bigger nudges go in judged steps no larger than this.
    JUDGE_STEP_DEG = 15.0

    def _nudge(self, direction, degrees=10):
        if self.arm is None:
            return "I have no arm attached.", None
        d = float(degrees or 10)
        here = self.arm.snapshot()
        self._hold(60.0)
        sideways = direction in ("left", "right")
        joint = "base_yaw" if sideways else "wrist"
        # +base_yaw turns the camera right, +wrist tilts it up
        # (perception/world.py).
        sign = 1.0 if direction in ("right", "up") else -1.0
        start = float(here[joint])
        target = start + sign * d
        limits = getattr(getattr(self.arm, "config", None), "soft_limits",
                         None)
        if callable(limits):
            lo, hi = limits(joint)
            target = max(lo, min(hi, target))
        if abs(target - start) < 1.0:
            # Asking the judge would only report "the camera didn't move".
            return (f"I can't turn any further {direction}.",
                    {"direction": direction, "degrees": 0.0, "verdict": None,
                     "missed": True})
        steps = max(1, math.ceil(abs(target - start) / self.JUDGE_STEP_DEG
                                 - 1e-9))
        verdicts, said = [], None
        for k in range(1, steps + 1):
            step_to = start + (target - start) * k / steps

            def do(t=step_to):
                if sideways:
                    self._yaw_to(t)
                else:
                    self.arm.set("wrist", t)
            said, v = self._judged(direction, do)
            verdicts.append(v)
            if said:
                break                # a step that went wrong ends the move
        return said, {"direction": direction,
                      "degrees": round(abs(target - start), 1),
                      "steps": steps, "verdict": _combined(verdicts)}

    def _move_joints(self, **joints):
        if self.arm is None:
            return "I have no arm attached.", None
        from avi_bot.perception import verify as verify_mod
        self._hold(60.0)
        before = dict(self.arm.snapshot())
        moved = {j: round(float(joints[j]), 1) for j in tools.JOINT_NAMES
                 if joints.get(j) is not None}

        # Judged by its larger change; clamped targets are what the arm
        # will actually do, so judge against those.
        lo_hi = getattr(getattr(self.arm, "config", None), "soft_limits",
                        None)
        expect = dict(before)
        for j, deg in moved.items():
            lo, hi = lo_hi(j) if callable(lo_hi) else (-1e9, 1e9)
            expect[j] = max(lo, min(hi, deg))
        direction = verify_mod.dominant(before, expect)
        # Like a big nudge, a big move goes in judged steps: base_yaw 90 ->
        # 60 in one go came back blind on the robot (2026-10-08).
        big = max(abs(expect[j] - before[j]) for j in ("base_yaw", "wrist")
                  if j in before)
        steps = (max(1, math.ceil(big / self.JUDGE_STEP_DEG - 1e-9))
                 if direction else 1)
        verdicts, said = [], None
        for k in range(1, steps + 1):
            pose = {j: before[j] + (expect[j] - before[j]) * k / steps
                    for j in moved}

            def do(pose=pose):
                for j, deg in pose.items():
                    if j == "base_yaw":
                        self._safe(lambda: self._yaw_to(deg))
                    else:
                        self._safe(lambda: self.arm.set(j, deg))
            said, v = self._judged(direction, do)
            verdicts.append(v)
            if said:
                break
        return said, {"moved": moved, "steps": steps,
                      "verdict": _combined(verdicts)}

    MAX_STEPS = 10

    def _sequence(self, steps):
        """Poses in order, each held, then back to where it started."""
        if self.arm is None:
            return "I have no arm attached.", None
        steps = [st for st in (steps or [])[:self.MAX_STEPS]
                 if isinstance(st, dict)]
        if not steps:
            return "Nothing to move.", None
        holds = [min(3.0, max(0.1, float(st.get("hold_s") or 0.5)))
                 for st in steps]
        start = dict(self.arm.snapshot())
        self._hold(sum(holds) + 30.0)

        def run():
            for st, hold in zip(steps, holds):
                for j in tools.JOINT_NAMES:
                    if st.get(j) is not None:
                        self._safe(lambda: self.arm.set(j, float(st[j])))
                self._sleep(hold)
            for j in tools.JOINT_NAMES:
                if j in start:
                    self._safe(lambda: self.arm.set(j, start[j]))

        if self.background:
            import threading
            threading.Thread(target=run, daemon=True).start()
        else:
            run()
        return None, {"steps": len(steps), "seconds": round(sum(holds), 1)}

    def _describe_view(self):
        """What the camera sees now, from the same labels the model gets --
        so the answer exists even when the model is out of reach."""
        seen = None
        if self.situation is not None:
            try:
                seen = (self.situation() or {}).get("camera")
            except Exception:
                seen = None
        if seen is None:
            return "I can't see right now.", None
        if not seen:
            return "Nothing I recognise.", {"camera": []}
        names = []
        for item in seen:
            label, _, where = item.partition(" (")
            where = where.split(",")[0]
            names.append(f"{'you' if label == 'person' else 'a ' + label}"
                         f" ({where})")
        return "I see " + ", ".join(names[:4]) + ".", {"camera": seen}

    # Base offsets for "look around": left, ahead, right of where it is.
    LOOK_AROUND_DEG = (-30.0, 0.0, 30.0)

    def _look_around(self, count=3):
        """Sweep left-centre-right, collect what the camera recognises at
        each stop, come back, and say up to `count` things with where."""
        if self.arm is None:
            return "I have no arm attached.", None
        if self.situation is None:
            return "I can't see right now.", None
        count = max(1, min(6, int(count or 3)))
        start = dict(self.arm.snapshot())
        lo, hi = self.arm.config.soft_limits("base_yaw")
        self._hold(90.0)
        seen, dim = {}, None
        for off in self.LOOK_AROUND_DEG:
            target = max(lo, min(hi, start["base_yaw"] + off))
            self._safe(lambda: self._yaw_to(target))
            self._sleep(0.8)
            try:
                sit = self.situation() or {}
            except Exception:
                sit = {}
            cam = sit.get("camera") or []
            dim = dim or sit.get("light")
            side = "left" if off < 0 else "right" if off > 0 else "ahead"
            for item in cam:
                label = item.split(" (")[0]
                seen.setdefault(label, side)
        self._safe(lambda: self._yaw_to(start["base_yaw"]))
        things = [(lb, w) for lb, w in seen.items() if lb != "person"]
        if "person" in seen:
            things.append(("person", seen["person"]))
        # Fewer than asked for is said, with the reason when it is the dark
        # (robot, 2026-10-08, brightness 39: "describe 3 things" got one).
        dark = " It's too dark to make out more." if dim else ""
        if not things:
            return ("I looked around and didn't recognise anything."
                    + dark), {"seen": {}, "light": dim}
        words = [("you" if lb == "person" else f"a {lb}") + f" ({w})"
                 for lb, w in things[:count]]
        lead = "I see " if len(things) >= count else "I can only make out "
        return (lead + ", ".join(words) + "."
                + (dark if len(things) < count else "")), {"seen": seen,
                                                          "light": dim}

    def _go_to_pose(self, name):
        if self.arm is None:
            return "I have no arm attached.", None
        self._hold(60.0)
        self._safe(lambda: self.arm.recall(name))
        return None, {"pose": name}

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

    def _modes_words(self):
        """'focus mode (12 of 25 min), quiet' -- what is on, for the
        model; None when nothing is."""
        on = []
        try:
            s = getattr(self.focus, "session", None)
            if s is not None and s.ended is None:
                m = int((self.clock() - s.started) // 60)
                on.append(f"focus mode ({m}"
                          + (f" of {int(s.minutes)}" if s.minutes else "")
                          + " min)")
            snap = self.modes.snapshot()
            if snap.get("asleep"):
                on.append("asleep")
            if snap.get("quiet"):
                on.append("quiet")
            if snap.get("guest"):
                on.append("guest mode (someone else is here)")
        except Exception:
            return None               # a turn is never lost to its context
        return ", ".join(on) or None

    # --- the two entry points --------------------------------------------
    def handle(self, utterance):
        """One turn: hear, route, execute, speak. Never raises."""
        sit = {}
        if self.situation is not None:
            try:
                sit = self.situation() or {}
            except Exception:
                sit = {}              # a turn is never lost to its context
        noticed = None
        if self.observed is not None:
            try:
                noticed = self.observed()
            except Exception:
                noticed = None
        ctx = context_mod.from_store(
            self.store, utterance=utterance,
            now=datetime.fromtimestamp(self.clock()),
            activity=sit.get("activity"), objects=sit.get("objects"),
            camera=sit.get("camera"), pose=sit.get("pose"),
            desk=sit.get("desk"), light=sit.get("light"),
            modes=self._modes_words(), noticed=noticed,
            mood=(self.mood.emotion() if self.mood else None),
            turns=self.turns[-6:])
        decision = self.router.handle(utterance, context=ctx)
        results = self.skills.run_all(decision.calls)

        # A skill that produced something sayable overrides the model's own
        # words, because the skill actually knows the answer and the model was
        # guessing at it.
        said = next((r.text for r in results if r.ok and r.text), None)
        if said:
            # A move the picture says did not happen is an error to own up
            # to, not an action to stay quiet about (perception/verify.py).
            # So is a look that found nothing (data "missed").
            missed = any(isinstance(r.data, dict)
                         and (r.data.get("missed")
                              or ((r.data.get("verdict") or {}).get("judged")
                                  and not r.data["verdict"].get("agrees")))
                         for r in results if r.ok)
            # An action whose words he needs ("Haven't seen it today. I'll
            # look.") is an answer, not chatter.
            tell = any(isinstance(r.data, dict) and r.data.get("tell")
                       for r in results if r.ok)
            decision.reply = self.policy.apply(
                decision.reply.model_copy(update={"say": said}),
                speech.ERROR if missed else
                speech.ANSWER if tell else decision.reason)

        # The mood's task events (BUILD_SPEC 5.2) had no caller: doing what
        # he asked lifts valence, failing lowers it. Praise and scold are
        # mood events themselves, and counting them twice would double them.
        acted = [(c[0], r) for c, r in zip(decision.calls, results)
                 if c[0] not in ("praise", "scold")]
        if acted:
            self._mood_event("task_ok" if all(r.ok for _, r in acted)
                             else "task_failed")

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
    # From this hour, the first sit-down gets tomorrow's heads-up.
    EVENING_HOUR = 17

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
        # Facts he told it that name today or tomorrow: "chemistry test on
        # Friday" sat in memory while Thursday's rundown said nothing.
        tomorrow_only = []
        try:
            from avi_bot.memory import dated
            today = habits.day_of(now)
            for d, words in dated.ahead(self.store.facts(), today):
                start = datetime(d.year, d.month, d.day).timestamp()
                later = d != today
                if not later and datetime.fromtimestamp(now).hour >= 12:
                    # A fact has a day, not a time: by an after-school
                    # sit-down, "Today: chemistry test" is likely over.
                    continue
                items.append((max(start, now) + (86400 if later else 0),
                              f"{words} tomorrow" if later else words))
                if later:
                    tomorrow_only.append(words)
        except Exception:
            pass                      # a misread fact never costs the rundown
        if datetime.fromtimestamp(now).hour >= self.EVENING_HOUR:
            # An evening rundown already says what is on tomorrow.
            self._heads_up_day = day
        if not items:
            return None
        if len(tomorrow_only) == len(items):
            # Nothing today, something tomorrow: not "Today: ... tomorrow."
            head = tomorrow_only[:self.RUNDOWN_ITEMS]
            more = len(items) - len(head)
            return ("Tomorrow: " + "; ".join(head)
                    + (f"; and {more} more." if more else "."))
        items.sort(key=lambda i: i[0])
        head = [w for _, w in items[:self.RUNDOWN_ITEMS]]
        more = len(items) - len(head)
        return ("Today: " + "; ".join(head)
                + (f"; and {more} more." if more else "."))

    def heads_up(self, now=None):
        """The night before: "Tomorrow: Math test." -- once, at his first
        sit-down from EVENING_HOUR, when the morning rundown was hours ago
        and tonight is when he would study for it. Facts that name tomorrow
        and reminders due tomorrow. None outside the evening, after it was
        said, or with nothing on.
        """
        from avi_bot.memory import dated
        from avi_bot.perception import habits
        now = self.clock() if now is None else float(now)
        if datetime.fromtimestamp(now).hour < self.EVENING_HOUR:
            return None
        day = habits.day_of(now)
        if getattr(self, "_heads_up_day", None) == day:
            return None
        self._heads_up_day = day
        start = habits.day_start_ts(now) + 86400.0
        items = []
        for r in self.store.reminders("pending"):
            if r.get("kind") != "timer" and start <= r["due_ts"] < start + \
                    86400.0:
                items.append((r["due_ts"], f"{r['text']} at "
                              f"{numbers.clock_time(r['due_ts'], am_pm=True)}"))
        try:
            for d, words in dated.ahead(self.store.facts(), day):
                if d != day:
                    items.append((start, words))
        except Exception:
            pass
        if not items:
            return None
        items.sort(key=lambda i: i[0])
        head = [w for _, w in items[:self.RUNDOWN_ITEMS]]
        more = len(items) - len(head)
        return ("Tomorrow: " + "; ".join(head)
                + (f"; and {more} more." if more else "."))

    def tick(self):
        """Called from the existing arm loop. Returns an Announcement or None."""
        return self.scheduler.next_announcement()

    def close(self):
        self.store.close()
        self.budget.close()
