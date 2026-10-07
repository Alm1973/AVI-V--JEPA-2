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
retires it if it goes stale -- the same rule it already applies to speech.
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
    "still": None, "breathe": None,
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

    def __init__(self, arm, recall, find, seat_view="ATTENTIVE"):
        self._arm = arm
        self._recall = recall          # recall(name) -> moves to a taught pose
        self._find = find              # find(name, look) -> dict with "say"
        self._seat = seat_view

    def __getattr__(self, name):
        # config, set, snapshot, play, roommap, desk... pass straight through.
        return getattr(self._arm, name)

    def recall(self, name):
        return self._recall(name)

    def cancel(self):
        self._arm.engine.clear()

    def look_at(self, target):
        if (target or "").strip().lower() in PEOPLE:
            return self._recall(self._seat)
        return self._find(target, True)

    def find_object(self, name, look=True):
        return self._find(name, look)


class Host:
    def __init__(self, make_brain, present=lambda: True, note=print,
                 cue=None, clock=time.time, perform=None):
        self._make = make_brain
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
        self.present = present
        self.note = note
        self.cue = cue
        self.clock = clock
        self.error = None
        self.said = []                 # recent announcements, newest last
        # Things AVI found out on its own and owes him, e.g. the result of a
        # search he asked for. Delivered like scheduler announcements: only
        # while he is at the desk.
        self.pending = []

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

    def ask(self, text):
        """One text turn. Never raises."""
        text = (text or "").strip()
        if not text:
            return {"error": "say something"}
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
        move = body_move(reply.gesture, reply.beep)
        if move and self.perform is not None:
            try:
                self.perform(move)
            except Exception as exc:
                self.note(f"reply gesture {move} failed: {exc!r}")
        out = {"say": reply.say, "gesture": move,
               "beep": reply.beep, "route": decision.route,
               "provider": decision.provider,
               "tools": [c[0] for c in decision.calls],
               "ok": all(r.ok for r in results)}
        err = getattr(decision, "error", None)
        if err:
            out["error"] = err
        if decision.route == "confused":
            # Key NAMES only, never values: enough to tell "no key on this
            # machine" from "the provider failed".
            out["keys_loaded"] = list(getattr(brain, "loaded_keys", []))
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

    def announce(self, text, kind="found"):
        """Queue something to tell him when he is next at the desk."""
        self.pending.append({"kind": kind, "text": text,
                             "at": round(self.clock(), 1)})
        del self.pending[:-10]

    def _deliver(self, brain):
        if self.pending and self.present():
            item = self.pending.pop(0)
            self.note(f"AVI would say: {item['text']}")
            if self.cue is not None:
                try:
                    self.cue()
                except Exception as exc:
                    self.note(f"announcement cue failed: {exc!r}")
            self.said.append(item)
            del self.said[:-20]
            return item["text"]
        ann = brain.tick()
        if ann is None or not self.present():
            return None
        self.note(f"AVI would say: {ann.text}")
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
        while time.time() < deadline:
            try:
                self.tick()
            except Exception as exc:
                self.note(f"brain tick failed: {exc!r}")
            time.sleep(period)


class _NoBrain:
    """Lets pending announcements go out before anyone has spoken to it."""

    def tick(self):
        return None
