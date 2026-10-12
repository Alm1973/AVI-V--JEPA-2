"""Per-frame observations in, events out. Pure Python: no cv2, no mediapipe.

The camera loop (assistant/senses.py) reduces each MediaPipe result to an
`Obs` and calls `Senses.update`. Everything that decides WHEN something counts
as having happened lives here, so it can be tested without a camera.

A sense that did not run on a frame is None; one that ran and saw nothing is
an empty list. The difference matters: hands run at 15 Hz and body at 5 Hz,
and a frame where body did not run is not evidence that nobody is there.

Rules carried over from the old pipeline, because each was learned the hard way:

  HYSTERESIS. One frame is not an arrival and one missed frame is not a
  departure. Arrival needs ARRIVE_S of near-continuous sightings; departure
  needs LEAVE_S with none.

  A POSTER IS NOT HIM (presence.py, 2026-10-09). A body box that has not
  moved for DECOY_STILL_S is remembered as a decoy and stops counting as a
  person. New here: a face that BLINKS is alive and can never become a decoy,
  and a body lying down is exempt -- otherwise he would be learned as a
  poster every night while asleep.
"""
import math
from dataclasses import dataclass, field

# --- presence --------------------------------------------------------------
ARRIVE_S = 3.0            # seen near-continuously this long -> arrived
ARRIVE_GAP_S = 1.0        # a gap longer than this restarts the arrival clock
LEAVE_S = 90.0            # not seen this long -> left (stamped at last sighting)

# --- decoys (posters, photos) -----------------------------------------------
DECOY_STILL_S = 300.0     # a body box this still for 5 min is a decoy
DECOY_TOL = 0.006         # centre may wander this far (fraction of frame)...
DECOY_SIZE_TOL = 0.012    # ...and width/height this far, and still be "still"
DECOY_MATCH = 0.02        # a later box this close to a decoy IS the decoy
DECOY_FORGET_AFTER = 3    # this many moved sightings on a decoy -> forget it

# --- face ------------------------------------------------------------------
LOOK_YAW_DEG = 20.0       # facing the camera within this...
LOOK_PITCH_DEG = 20.0     # ...and this counts as looking at AVI
LOOK_HOLD_S = 1.0         # held this long -> looked_at_avi
LOOK_COOLDOWN_S = 30.0
YAWN_JAW = 0.5            # jawOpen blendshape
YAWN_HOLD_S = 1.5
YAWN_COOLDOWN_S = 10.0
BLINK = 0.5               # mean eyeBlink blendshape
EYES_CLOSED_S = 5.0       # closed this long -> eyes_closed (dozing / asleep)
BLINK_MAX_S = 0.6         # closed and open again within this -> a blink
OTHERS_HOLD_S = 3.0       # two or more faces this long -> others_present

# --- hands -----------------------------------------------------------------
GESTURE_SCORE = 0.6
GESTURE_VOTES = 4         # of the last GESTURE_WINDOW hand frames
GESTURE_WINDOW = 5
GESTURE_COOLDOWN_S = 3.0

# --- lights ----------------------------------------------------------------
LIGHTS_DEFAULT = 40.0     # brightness threshold until one is learned
LIGHTS_HOLD_S = 10.0      # across the threshold this long -> lights_on/off
LIGHTS_MIN_SAMPLES = 600  # brightness samples before a learned threshold is trusted
LIGHTS_MIN_MODE = 0.05    # each side of a learned threshold needs this share
LIGHTS_MIN_GAP = 30.0     # and the two sides' means must be this far apart

SAMPLE_EVERY_S = 10.0     # one time-series row this often, event or not


@dataclass
class Body:
    box: tuple            # (cx, cy, w, h) as fractions of the frame
    lying: bool | None    # None when shoulders/hips are not visible


@dataclass
class Face:
    box: tuple
    yaw: float            # degrees, 0 = facing the camera
    pitch: float
    jaw: float            # jawOpen blendshape 0..1
    blink: float          # mean of eyeBlinkLeft/Right 0..1


@dataclass
class Hand:
    gesture: str          # MediaPipe's top category, "None" when none
    score: float
    side: str = ""        # "Left" / "Right" as MediaPipe reports it


@dataclass
class Obs:
    ts: float
    brightness: float
    bodies: list | None = None
    faces: list | None = None
    hands: list | None = None


@dataclass
class Event:
    ts: float
    kind: str
    detail: dict = field(default_factory=dict)


def otsu(hist):
    """Threshold splitting a 256-bin histogram into two classes, plus
    (share_low, share_high, mean_low, mean_high). None if empty."""
    total = sum(hist)
    if total == 0:
        return None
    sum_all = sum(i * h for i, h in enumerate(hist))
    best, best_t = -1.0, None
    w0 = s0 = 0.0
    for t in range(256):
        w0 += hist[t]
        s0 += t * hist[t]
        w1 = total - w0
        if w0 == 0 or w1 == 0:
            continue
        m0, m1 = s0 / w0, (sum_all - s0) / w1
        between = w0 * w1 * (m0 - m1) ** 2
        if between > best:
            best, best_t = between, (t + 0.5, w0 / total, w1 / total, m0, m1)
    return best_t


class Lights:
    """Lights on/off, with the threshold learned from AVI's own history.

    Brightness in a lit room and a dark one forms two humps; Otsu's method
    finds the valley. Until both humps have been seen, a fixed default is
    used and the event says so (`learned: false`)."""

    def __init__(self, hist=None):
        self.hist = list(hist) if hist else [0] * 256
        self.on = None
        self.cross_since = None

    def threshold(self):
        if sum(self.hist) >= LIGHTS_MIN_SAMPLES:
            r = otsu(self.hist)
            if r and min(r[1], r[2]) >= LIGHTS_MIN_MODE and r[4] - r[3] >= LIGHTS_MIN_GAP:
                return r[0], True
        return LIGHTS_DEFAULT, False

    def update(self, ts, brightness):
        self.hist[max(0, min(255, int(brightness)))] += 1
        thr, learned = self.threshold()
        now_on = brightness >= thr
        if self.on is None:
            self.on = now_on
            return None
        if now_on == self.on:
            self.cross_since = None
            return None
        if self.cross_since is None:
            self.cross_since = ts
        if ts - self.cross_since >= LIGHTS_HOLD_S:
            self.on, self.cross_since = now_on, None
            return Event(ts, "lights_on" if now_on else "lights_off",
                         {"brightness": round(brightness, 1), "threshold": round(thr, 1),
                          "learned": learned})
        return None


class Decoys:
    """Body boxes that are not a person: learned from stillness, unlearned
    from movement. The blink and lying-down exemptions are applied by the
    caller, which knows about faces."""

    def __init__(self, known=None):
        self.known = [dict(d, moved=d.get("moved", 0)) for d in (known or [])]
        self.track = None          # (box, still_since) of the current candidate

    @staticmethod
    def _close(a, b, tol, size_tol):
        return (abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol
                and abs(a[2] - b[2]) <= size_tol and abs(a[3] - b[3]) <= size_tol)

    def is_decoy(self, box):
        for d in self.known:
            if self._close(box, d["box"], DECOY_MATCH, DECOY_MATCH):
                d["moved"] = 0
                return True
            # Overlapping a decoy but moved: someone alive is there.
            if self._close(box, d["box"], 0.15, 0.15):
                d["moved"] += 1
                if d["moved"] >= DECOY_FORGET_AFTER:
                    self.known.remove(d)
                return False
        return False

    def watch(self, ts, box, exempt):
        """Feed the main body box. Returns the box when newly learned."""
        if exempt or box is None:
            self.track = None
            return None
        if self.track and self._close(box, self.track[0], DECOY_TOL, DECOY_SIZE_TOL):
            if ts - self.track[1] >= DECOY_STILL_S:
                self.known.append({"box": self.track[0], "moved": 0})
                self.track = None
                return box
        else:
            self.track = (box, ts)
        return None


class Held:
    """A condition that must hold for `hold` seconds to fire, then cools down."""

    def __init__(self, hold, cooldown):
        self.hold, self.cooldown = hold, cooldown
        self.since = None
        self.last_fire = -math.inf

    def update(self, ts, cond):
        if not cond:
            self.since = None
            return False
        if self.since is None:
            self.since = ts
        if ts - self.since >= self.hold and ts - self.last_fire >= self.cooldown:
            self.last_fire = ts
            self.since = None
            return True
        return False


class Senses:
    def __init__(self, lights_hist=None, decoys=None):
        self.lights = Lights(lights_hist)
        self.decoys = Decoys(decoys)
        self.present = False
        self.run_start = None      # start of the current near-continuous sighting
        self.last_seen = None
        self.arrived_at = None
        self.look = Held(LOOK_HOLD_S, LOOK_COOLDOWN_S)
        self.yawn = Held(YAWN_HOLD_S, YAWN_COOLDOWN_S)
        self.others = Held(OTHERS_HOLD_S, 300.0)
        self.closed_since = None
        self.eyes_closed = False
        self.alive_face = False    # a blink was seen on the current face
        self.gesture_votes = []
        self.gesture_last = {}
        self.gesture_held = None   # fired already; must leave the window to fire again
        self.next_sample = None
        self.window = self._empty_window()

    @staticmethod
    def _empty_window():
        return {"frames": 0, "present": 0, "lying": 0, "looking": 0,
                "hands": 0, "faces_max": 0, "brightness": 0.0, "decoys": 0}

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _main(items):
        return max(items, key=lambda x: x.box[2] * x.box[3]) if items else None

    def _looking(self, face):
        return (face is not None and abs(face.yaw) <= LOOK_YAW_DEG
                and abs(face.pitch) <= LOOK_PITCH_DEG)

    # -- the step ------------------------------------------------------------
    def update(self, o):
        out = []
        w = self.window
        w["frames"] += 1
        w["brightness"] += o.brightness

        ev = self.lights.update(o.ts, o.brightness)
        if ev:
            out.append(ev)

        face = self._main(o.faces) if o.faces is not None else None
        if o.faces is not None:
            out += self._face(o.ts, face, len(o.faces))
            w["faces_max"] = max(w["faces_max"], len(o.faces))
            w["looking"] += self._looking(face)

        body = None
        if o.bodies is not None:
            real = [b for b in o.bodies if not self.decoys.is_decoy(b.box)]
            w["decoys"] += len(o.bodies) - len(real)
            body = self._main(real)
            exempt = self.alive_face or (body is not None and body.lying)
            learned = self.decoys.watch(o.ts, body.box if body else None, exempt)
            if learned:
                out.append(Event(o.ts, "learned_decoy", {"box": [round(v, 3) for v in learned]}))
                body = None
            w["lying"] += bool(body and body.lying)

        if o.bodies is not None or o.faces is not None:
            seen = body is not None or face is not None
            w["present"] += seen
            out += self._presence(o.ts, seen)

        if o.hands is not None:
            w["hands"] += bool(o.hands)
            out += self._hands(o.ts, o.hands, face)

        if self.next_sample is None:
            self.next_sample = o.ts + SAMPLE_EVERY_S
        return out

    def _presence(self, ts, seen):
        out = []
        if seen:
            if self.last_seen is None or ts - self.last_seen > ARRIVE_GAP_S:
                self.run_start = ts
            self.last_seen = ts
            if not self.present and ts - self.run_start >= ARRIVE_S:
                self.present, self.arrived_at = True, self.run_start
                out.append(Event(self.run_start, "arrived"))
        elif self.present and ts - self.last_seen >= LEAVE_S:
            self.present = False
            mins = (self.last_seen - self.arrived_at) / 60.0
            out.append(Event(self.last_seen, "left", {"minutes": round(mins, 1)}))
        return out

    def _face(self, ts, face, n):
        out = []
        if face is None:
            self.alive_face = False
            self.closed_since = None
        else:
            closed = face.blink >= BLINK
            if closed and self.closed_since is None:
                self.closed_since = ts
            if not closed and self.closed_since is not None:
                dur = ts - self.closed_since
                if dur <= BLINK_MAX_S:
                    self.alive_face = True
                if self.eyes_closed:
                    self.eyes_closed = False
                    out.append(Event(ts, "eyes_opened", {"closed_s": round(dur, 1)}))
                self.closed_since = None
            if closed and not self.eyes_closed and ts - self.closed_since >= EYES_CLOSED_S:
                self.eyes_closed = True
                out.append(Event(self.closed_since, "eyes_closed"))
            if self.yawn.update(ts, face.jaw >= YAWN_JAW):
                out.append(Event(ts, "yawn"))
        if self.look.update(ts, self._looking(face)):
            out.append(Event(ts, "looked_at_avi"))
        if self.others.update(ts, n >= 2):
            out.append(Event(ts, "others_present", {"faces": n}))
        return out

    def _hands(self, ts, hands, face):
        best = max(hands, key=lambda h: h.score, default=None)
        name = best.gesture if best and best.score >= GESTURE_SCORE and best.gesture != "None" else None
        self.gesture_votes = (self.gesture_votes + [name])[-GESTURE_WINDOW:]
        # One hold is one gesture: a thumbs-up kept up for ten seconds fired
        # every cooldown on the sample photos, 2026-10-12. It re-arms only
        # once the gesture has left the vote window entirely.
        if self.gesture_held and self.gesture_held not in self.gesture_votes:
            self.gesture_held = None
        if name and name != self.gesture_held and self.gesture_votes.count(name) >= GESTURE_VOTES \
                and ts - self.gesture_last.get(name, -math.inf) >= GESTURE_COOLDOWN_S:
            self.gesture_last[name] = ts
            self.gesture_held = name
            return [Event(ts, "gesture", {"name": name, "hand": best.side,
                                          "facing_avi": self._looking(face)})]
        return []

    def sample(self, ts):
        """The time-series row for the last SAMPLE_EVERY_S, or None if not due.
        Written whether or not anything happened: absence of change is data."""
        if self.next_sample is None or ts < self.next_sample:
            return None
        w, n = self.window, max(1, self.window["frames"])
        row = {"ts": ts, "present": int(self.present),
               "seen_frac": round(w["present"] / n, 3),
               "lying_frac": round(w["lying"] / n, 3),
               "looking_frac": round(w["looking"] / n, 3),
               "hands_frac": round(w["hands"] / n, 3),
               "faces_max": w["faces_max"],
               "brightness": round(w["brightness"] / n, 1),
               "lights_on": None if self.lights.on is None else int(self.lights.on),
               "decoy_hits": w["decoys"]}
        self.window = self._empty_window()
        self.next_sample = ts + SAMPLE_EVERY_S
        return row
