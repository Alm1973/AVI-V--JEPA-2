"""The voice brain, hosted in the dashboard with the real arm.

avi_bot.main.Avi -- the router, 27 tools, timers, reminders, focus, the
scheduler -- was complete and tested, and ran nowhere: nothing constructed it
with an arm. Everything the surveys learned about where things are was
reachable only through /find, and a timer set by voice could not exist.

This hosts it inside the dashboard process, which owns the arm and the map:
  * BrainArm adapts the dashboard's Arm to the calls the brain makes
    (recall a taught pose, cancel, look at someone, find a thing).
  * Host builds the brain on first use, answers text turns (POST /ask -- the
    microphone feeds the same call once the reSpeaker is in), and delivers
    what the scheduler wants to say.

Delivery is honest about the missing speakers: an announcement is shown on
the dashboard and cued with a gesture, and only while he is at the desk. A
cue to an empty room is not a delivery, so it stays pending and the scheduler
retires it if it goes stale -- the same rule it already applies to speech --
except a reminder, which is held as missed and told when he is back.
"""
import threading
import time

# Words that mean "look at him", not at an object called that.
PEOPLE = {"me", "him", "shaurya", "you", "person", "here", "over here"}

# Played with an announcement until there is a speaker to say it.
CUE_GESTURE = "ahem"

# The brain may ask for 19 gestures (schema.GESTURES); the arm's library has
# 13, and a name it does not have fails in gestures.make. Nearest move it can
# actually play; None means "nothing", which is what "still" asks for.
ARM_GESTURE = {
    "nod": "nod", "shake": "shake", "perk_up": "perk_up", "droop": "droop",
    "celebrate": "celebrate", "ahem": "ahem", "yawn_wake": "yawn_wake",
    "thinking": "thinking", "embarrassed": "embarrassed",
    "curious_lean": "curious_lean",
    "tilt": "curious_lean", "lean_in": "curious_lean",
    "shrug": "embarrassed", "look_around": "thinking", "greet": "perk_up",
    "glance_at_you": "perk_up", "fidget": "excited_wiggle",
    "still": None, "breathe": None, "wave": "wave",
}

# Until the speakers arrive a beep is silence, so "timer set" acknowledged
# nothing at all. Each beep's emotion as the gesture that reads the same.
BEEP_GESTURE = {
    "neutral": "nod", "happy": "nod", "affectionate": "nod",
    "excited": "excited_wiggle", "proud": "celebrate",
    "curious": "curious_lean", "alert": "perk_up",
    "sad": "droop", "sulky": "droop", "confused": "shake", "sleepy": "doze",
}


def body_move(gesture, beep, have_speaker=False):
    """The arm gesture for a reply, or None."""
    if gesture:
        return ARM_GESTURE.get(gesture)
    if beep and not have_speaker:
        return BEEP_GESTURE.get(beep)
    return None


class BrainArm:
    """The dashboard Arm, as avi_bot.main.Avi expects to drive it."""

    def __init__(self, arm, recall, find, seat_view="ATTENTIVE",
                 look_at_person=None):
        self._arm = arm
        self._recall = recall          # recall(name) -> moves to a taught pose
        self._find = find              # find(name, look) -> dict with "say"
        self._seat = seat_view
        # () -> centre him in the picture; None = just the seat pose.
        self._look_at_person = look_at_person

    def __getattr__(self, name):
        # config, set, snapshot, play, roommap, desk... pass straight through.
        return getattr(self._arm, name)

    def recall(self, name):
        return self._recall(name)

    def cancel(self):
        self._arm.engine.clear()

    def look_at(self, target):
        if (target or "").strip().lower() in PEOPLE:
            if self._look_at_person is not None:
                return self._look_at_person()
            return self._recall(self._seat)
        return self._find(target, True)

    def find_object(self, name, look=True):
        return self._find(name, look)


class Host:
    def __init__(self, make_brain, present=lambda: True, note=print,
                 cue=None, clock=time.time, perform=None, speaker=None,
                 eager=False):
        self._make = make_brain
        # Build the brain when the loop starts rather than on the first /ask.
        # Reminders live in the brain's store and only its scheduler fires
        # them, so a lazily built brain meant: after a restart, nothing he
        # asked to be reminded of came due until he happened to ask AVI
        # something else first.
        self.eager = eager
        # voice/output.Speaker, or None. When it is switched on, words are
        # spoken and beeps chirped, and a beep no longer needs a gesture to
        # stand in for it.
        self.speaker = speaker
        # Plays a reply's gesture on the arm. A reply is say + beep + gesture,
        # and before this only the words came back: the shake of a confused
        # turn, the nod of a timer set, were decided and then dropped.
        self.perform = perform
        self._brain = None
        self._lock = threading.Lock()
        # One turn at a time: requests arrive on their own threads and the
        # scheduler ticks on another, and the brain's stores are one sqlite
        # connection each.
        self._turn = threading.Lock()
        self._here = present
        self.last_ask = None           # when he last typed or said something
        self.note = note
        self.cue = cue
        self.clock = clock
        self.error = None
        self.said = []                 # recent announcements, newest last
        # Things AVI found out on its own and owes him, e.g. the result of a
        # search he asked for. Delivered like scheduler announcements: only
        # while he is at the desk.
        self.pending = []

    # Someone who asked something this recently is around, whatever the
    # seat camera says. Presence only counts looks from the seat view, and
    # while he drove the arm around it never was: "set a timer for 1
    # minute" was announced 8 minutes late, when presence caught up
    # (robot, 2026-10-08).
    ENGAGED_S = 600.0

    def present(self):
        if self.last_ask is not None and (self.clock() - self.last_ask
                                          < self.ENGAGED_S):
            return True
        return bool(self._here())

    @property
    def brain(self):
        with self._lock:
            if self._brain is None and self.error is None:
                try:
                    self._brain = self._make()
                except Exception as exc:       # keep the dashboard up
                    self.error = repr(exc)
                    self.note(f"brain failed to start: {self.error}")
            return self._brain

    def run_tool(self, name, args):
        """One skill straight from a button, no model: the dances panel's
        play, and remote tests that must not spend the free budget."""
        brain = self.brain
        if brain is None:
            return {"error": f"brain unavailable: {self.error}"}
        try:
            with self._turn:
                r = brain.skills.run(name, dict(args or {}))
        except Exception as exc:
            return {"error": repr(exc)}
        return {"ok": r.ok, "error": r.error, "say": r.text,
                "data": r.data if isinstance(r.data, dict) else None}

    def moves(self):
        """The dances AVI made up, best liked first."""
        brain = self.brain
        if brain is None:
            return []
        try:
            return brain.store.moves_known(limit=20)
        except Exception:
            return []

    def forget_move(self, name):
        brain = self.brain
        if brain is None:
            return 0
        with self._turn:
            return brain.store.drop_move(name)

    # Tools that play their own motion over seconds.
    OWN_MOTION = ("perform", "motion_sequence")

    def ask(self, text):
        """One text turn. Never raises."""
        text = (text or "").strip()
        if not text:
            return {"error": "say something"}
        self.last_ask = self.clock()
        brain = self.brain
        if brain is None:
            return {"error": f"brain unavailable: {self.error}"}
        try:
            with self._turn:
                decision, results = brain.handle(text)
        except Exception as exc:
            self.note(f"brain turn failed: {exc!r}")
            return {"error": repr(exc)}
        reply = decision.reply
        voiced = bool(self.speaker is not None and self.speaker.enabled())
        if voiced:
            if reply.say:
                self.speaker.say_later(reply.say)
            elif reply.beep:
                self.speaker.chirp_later(reply.beep)
        move = body_move(reply.gesture, reply.beep, have_speaker=voiced)
        if any(c[0] in self.OWN_MOTION for c in decision.calls):
            # The turn already moves the arm for a while; a reply gesture
            # pushed on top would cut into the dance it asked for.
            move = None
        if move and self.perform is not None:
            try:
                self.perform(move)
            except Exception as exc:
                self.note(f"reply gesture {move} failed: {exc!r}")
        out = {"say": reply.say, "gesture": move,
               "beep": reply.beep, "route": decision.route,
               "provider": decision.provider,
               "tools": [c[0] for c in decision.calls],
               # What it DID, with the camera's verdict on each move -- so
               # the page can show more than "(done)".
               "did": [{"tool": c[0], "args": c[1], "ok": r.ok,
                        "error": r.error,
                        "verdict": _brief(r.data.get("verdict")
                                          if isinstance(r.data, dict)
                                          else None),
                        # A look's own check: was the thing in the picture?
                        "in_view": (r.data.get("in_view")
                                    if isinstance(r.data, dict) else None)}
                       for c, r in zip(decision.calls, results)],
               "ok": all(r.ok for r in results)}
        err = getattr(decision, "error", None)
        if err:
            out["error"] = err
        if decision.route == "confused":
            # Key NAMES only, never values: enough to tell "no key on this
            # machine" from "the provider failed".
            out["keys_loaded"] = list(getattr(brain, "loaded_keys", []))
        miss = getattr(getattr(brain, "router", None), "last_cloud_miss",
                       None)
        if miss and decision.route != "cloud":
            self.note(f"model skipped for {text!r}: {miss}")
            brain.router.last_cloud_miss = None
            # For the page: answered by the quick rules because the model
            # could not be reached or had run out of free requests -- so a
            # dumber answer is explained rather than a mystery.
            out["fallback"] = True
        self.note(f"ask {text!r} -> {reply.say!r} via {decision.route}")
        return out

    def tick(self):
        """Deliver at most one announcement. Returns its text, or None."""
        brain = self._brain                  # never construct from the loop
        if brain is None:
            if not self.pending:
                return None
            brain = _NoBrain()
        with self._turn:
            return self._deliver(brain)

    def _speak(self, text):
        if self.speaker is not None and self.speaker.enabled():
            self.speaker.say_later(text)

    # --- focus mode, connected to what the camera sees ------------------
    # skills/focus.py decides when to nudge -- phone time is evidence, a
    # settled pause is the trigger -- but nothing fed it either, and a timed
    # session never ended: "focus for 25 minutes" ran forever, silently.
    FOCUS_PHONE_WORDS = "Phone's been out a while."
    FOCUS_BREAK_WORDS = "Good moment for a break."
    FOCUS_BREAK_GESTURE = "yawn_wake"

    def _focus(self):
        focus = getattr(self._brain, "focus", None)
        return focus if focus is not None and focus.active else None

    def focus_active(self):
        return self._focus() is not None

    def saw_phone(self, on, now=None):
        """A phone verdict from the detector. Evidence only."""
        focus = self._focus()
        if focus is not None:
            with self._turn:
                focus.saw_phone(bool(on), now=now)

    def focus_tick(self, breakpoint=False, now=None):
        """End a timed session, or let focus decide on a nudge and do it.
        Returns "done", a focus nudge kind, or None."""
        from avi_bot.skills import focus as focus_mod
        focus = self._focus()
        if focus is None:
            return None
        now = self.clock() if now is None else now
        with self._turn:
            s = focus.session
            if s.minutes and now - s.started >= float(s.minutes) * 60.0:
                text, _ = focus.stop()
                done = True
            else:
                kind = focus.tick(now=now, breakpoint=breakpoint)
                done = False
        if done:
            self.announce(f"Focus done. {text}", kind="focus")
            return "done"
        if kind == focus_mod.AHEM:
            self._move(CUE_GESTURE)              # a glance, no words
        elif kind == focus_mod.WORDS:
            self.announce(self.FOCUS_PHONE_WORDS, kind="focus")
        elif kind == focus_mod.BREAK:
            self._move(self.FOCUS_BREAK_GESTURE)
            self.announce(self.FOCUS_BREAK_WORDS, kind="focus")
        return kind

    def _move(self, gesture):
        if self.perform is None:
            return
        try:
            self.perform(gesture)
        except Exception as exc:
            self.note(f"focus gesture {gesture} failed: {exc!r}")

    def arrived(self, first_today=True):
        """He sat down. The first time each day, tell him what is ahead
        (Avi.rundown); after that, the first time in the evening, what is
        on tomorrow (Avi.heads_up). Returns the text queued, or None."""
        brain = self._brain
        what = "rundown" if first_today else "heads_up"
        if brain is None or not hasattr(brain, what):
            return None
        try:
            with self._turn:
                text = getattr(brain, what)()
        except Exception as exc:
            self.note(f"{what} failed: {exc!r}")
            return None
        if text:
            self.announce(text, kind=what)
        return text

    def announce(self, text, kind="found"):
        """Queue something to tell him when he is next at the desk."""
        self.pending.append({"kind": kind, "text": text,
                             "at": round(self.clock(), 1)})
        # Bounded, but never by dropping a missed reminder: those are owed,
        # and the store still says "missed" until one is told.
        extra = len(self.pending) - 10
        if extra > 0:
            drop = [p for p in self.pending
                    if p.get("kind") != self.MISSED][:extra]
            self.pending = [p for p in self.pending if p not in drop]

    # --- reminders that came due while he was out ----------------------
    # The scheduler keeps a stale reminder as "missed" (scheduler.MISSED)
    # rather than dropping it, and a reminder that comes due to an empty
    # room is held the same way. Both live in the brain's store, so they
    # survive a restart; here they join `pending` and are told -- "while you
    # were away" -- when he is next at the desk.
    MISSED = "missed"

    @staticmethod
    def _missed_text(text, due_ts):
        from avi_bot import numbers
        when = (f" at {numbers.clock_time(due_ts, am_pm=True)}"
                if due_ts else "")
        return f"While you were away{when}: {text}"

    def _sync_missed(self, brain):
        sched = getattr(brain, "scheduler", None)
        if sched is None or not hasattr(sched, "missed_reminders"):
            return
        held = {p.get("ref") for p in self.pending
                if p.get("kind") == self.MISSED}
        for r in sched.missed_reminders():
            if r["id"] not in held:
                self.pending.append({
                    "kind": self.MISSED, "ref": r["id"],
                    "text": self._missed_text(r["text"], r.get("due_ts")),
                    "at": round(self.clock(), 1)})

    def _deliver(self, brain):
        self._sync_missed(brain)
        # What the brain found out after a turn had already answered, e.g.
        # that a dance it played never visibly moved the camera.
        owed = getattr(brain, "owed", None)
        while isinstance(owed, list) and owed:
            self.announce(owed.pop(0), kind="perform")
        sched = getattr(brain, "scheduler", None)
        muted = bool(sched is not None and hasattr(sched, "muted")
                     and sched.muted())
        # Quiet, asleep and guest mode hold what AVI found out on its own
        # too, not only the scheduler's announcements.
        if self.pending and self.present() and not muted:
            item = self.pending.pop(0)
            self.note(f"AVI would say: {item['text']}")
            if item.get("kind") == self.MISSED:
                sched.told_missed(item["ref"])
            self._speak(item["text"])
            if self.cue is not None:
                try:
                    self.cue()
                except Exception as exc:
                    self.note(f"announcement cue failed: {exc!r}")
            self.said.append(item)
            del self.said[:-20]
            return item["text"]
        ann = brain.tick()
        if ann is None:
            return None
        if not self.present():
            # A timer to an empty room goes stale and is dropped (not news
            # later); a reminder is held and told when he is back.
            if getattr(ann, "kind", None) == "reminder" and hasattr(
                    brain.scheduler, "hold"):
                brain.scheduler.hold(ann)
                self.note(f"reminder held until he is back: {ann.text!r}")
            return None
        self.note(f"AVI would say: {ann.text}")
        self._speak(ann.text)
        if self.cue is not None:
            try:
                self.cue()
            except Exception as exc:
                self.note(f"announcement cue failed: {exc!r}")
        brain.scheduler.delivered_ok(ann)
        self.said.append({"at": round(self.clock(), 1), "kind": ann.kind,
                          "text": ann.text})
        del self.said[:-20]
        return ann.text

    def loop(self, deadline, period=5.0):
        if self.eager:
            self.brain                       # builds it; errors are recorded
        while time.time() < deadline:
            try:
                self.focus_tick()              # a timed session ends on time
                self.tick()
            except Exception as exc:
                self.note(f"brain tick failed: {exc!r}")
            time.sleep(period)


def _brief(verdict):
    """A move verdict without the per-token detail, for the page."""
    if not verdict:
        return None
    return {k: verdict.get(k) for k in ("commanded", "votes", "judged",
                                        "agrees", "motion", "retried",
                                        "steps")
            if k in verdict}


class _NoBrain:
    """Lets pending announcements go out before anyone has spoken to it."""

    def tick(self):
        return None
