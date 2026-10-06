"""Tell a person in the room from the robot's own hardware in shot.

The camera is bolted to the arm, so anything else bolted to the arm -- a servo
horn, a cable loop, the bracket's edge -- sits at a FIXED position in the image
however far the arm swings. A real person does the opposite: pan the base by
`d` degrees and they slide across the frame by `d / yaw_deg_per_frame` of its
width.

Measured on 2026-10-05, and the reason this file exists: base_yaw swung from 40
to 57 degrees -- 17 degrees, which is 0.59 of a frame, about 750 px at 1280 --
while a detection box sat at x = 1035 +- 25 px. That box scored 0.56-0.72 and
Shaurya scored 0.75-0.89, two ranges close enough that a confidence floor
cannot separate them reliably: at 0.70 the hardware got through and the tracker
locked onto itself, and at 0.75 the detector reported 0 hits in 84 frames.
Parallax separates the same two things by a factor of thirty.

What this module does NOT do is decide whether a detection is a person. It
answers one narrow question -- does this image position move when the camera
does -- and the answer is only ever used to REJECT. A candidate nothing is
known about passes, because the cost of dropping a real frame is one missed
update on a gaze that is sticky for two seconds, and the cost of accepting the
arm is a feedback loop chasing an error it can never reduce.
"""
import time

# How far the base must have turned between two sightings before their
# separation says anything. Below this the expected shift is comparable to
# detection-box jitter (+-25 px measured) and the test would be noise.
MIN_EXPECTED_PX = 200.0

# A sighting pair counts as static when it moved less than this fraction of
# what the pan should have moved it. Well clear of both ends: real targets come
# in near 1.0, the arm's own hardware near 0.03.
STATIC_FRACTION = 0.25

# Only MAGNITUDES are compared, never directions. "Did it move" is the whole
# question, and a person walking the other way is still moving. The one case
# this misreads is someone travelling with the pan at exactly the rate that
# holds them still in frame, which is transient and clears itself the moment
# they stop.
# ... and as confirmed real above this fraction, which is what clears a zone
# again. The gap between the two is deliberate: a candidate in between leaves
# the zone exactly as it was rather than flipping it on weak evidence.
MOVING_FRACTION = 0.60

# Two sightings this close in the image are treated as the same thing. Sized
# from box jitter, not from the object.
SAME_PX = 60.0

# A static zone stops being trusted this long after the evidence for it. The
# arm's hardware does not move, but the camera's tilt and the arm's pose change
# which part of it is in frame, so a zone learned in one pose should not
# silently censor a different one forever.
ZONE_TTL_S = 180.0

# Sightings older than this are dropped. Long enough to span a slow pan, short
# enough that a person who has walked across the room is not compared against
# where they used to be.
HISTORY_S = 20.0


class Sighting:
    __slots__ = ("t", "yaw", "cx", "width")

    def __init__(self, t, yaw, cx, width):
        self.t = float(t)
        self.yaw = float(yaw)
        self.cx = float(cx)
        self.width = float(width)


class StaticZone:
    __slots__ = ("cx", "at", "evidence")

    def __init__(self, cx, at, evidence):
        self.cx = float(cx)
        self.at = float(at)
        self.evidence = evidence      # (d_yaw, d_cx, expected_px) that proved it

    def __repr__(self):
        d_yaw, d_cx, exp = self.evidence
        return (f"<StaticZone x={self.cx:.0f} "
                f"yaw moved {d_yaw:+.1f} deg, box moved {d_cx:+.0f} px of "
                f"{exp:.0f} expected>")


class StaticRejector:
    def __init__(self, yaw_deg_per_frame, clock=time.time):
        self.yaw_deg_per_frame = float(yaw_deg_per_frame)
        self.clock = clock
        self.history = []
        self.zones = []
        self.rejected = 0

    # --- geometry --------------------------------------------------------
    def expected_px(self, d_yaw, width):
        """How far a real-world point should slide for a `d_yaw` degree pan."""
        if not self.yaw_deg_per_frame:
            return 0.0
        return abs(d_yaw) / self.yaw_deg_per_frame * float(width)

    # --- learning --------------------------------------------------------
    def observe(self, yaw, cx, width, t=None):
        """Record a sighting and update what is known about static positions.

        Two separate questions, against two different partners in the history,
        because they need different partners to mean anything:

        STATIC asks "has something sat at this image position across a real
        pan". That needs the partner at the SAME image position with the
        LARGEST yaw difference available -- the biggest pan is the strongest
        evidence, and an earlier version that simply took the first matching
        entry compared a 1-degree pan and learned nothing.

        MOVING asks "did this thing slide the way a real object would". That
        needs the partner NEAREST IN TIME, because it is a claim about one
        object's motion, and over a long gap there is no reason to think the
        two sightings are the same object at all.

        Returns the StaticZone this sighting created or refreshed, or None.
        """
        t = self.clock() if t is None else float(t)
        now = Sighting(t, yaw, cx, width)
        self._expire(t)

        found = None
        same_place = [o for o in self.history if abs(o.cx - now.cx) <= SAME_PX]
        if same_place:
            best = max(same_place, key=lambda o: abs(now.yaw - o.yaw))
            d_yaw = now.yaw - best.yaw
            expected = self.expected_px(d_yaw, width)
            if expected >= MIN_EXPECTED_PX:
                moved = abs(now.cx - best.cx)
                if moved / expected < STATIC_FRACTION:
                    found = self._mark((best.cx + now.cx) / 2.0, t,
                                       (d_yaw, now.cx - best.cx, expected))

        if found is None:
            self._clear_if_moving(now)
        self.history.append(now)
        return found

    def _clear_if_moving(self, now):
        """A candidate that slid as a real object would clears its zone.

        Nearest in time, and only that one: this is a statement about a single
        object's motion between two frames, so comparing against a sighting
        from fifteen seconds ago would be comparing two different things.
        """
        if not self.history:
            return
        prev = max(self.history, key=lambda o: o.t)
        d_yaw = now.yaw - prev.yaw
        expected = self.expected_px(d_yaw, now.width)
        if expected < MIN_EXPECTED_PX:
            return
        if abs(now.cx - prev.cx) / expected < MOVING_FRACTION:
            return
        # It moved. Clear zones at either end of the slide, since whatever is
        # there demonstrably travels with the camera.
        self.zones = [z for z in self.zones
                      if abs(z.cx - prev.cx) > SAME_PX
                      and abs(z.cx - now.cx) > SAME_PX]

    def _mark(self, cx, t, evidence):
        for z in self.zones:
            if abs(z.cx - cx) <= SAME_PX:
                z.cx = (z.cx + cx) / 2.0
                z.at = t
                z.evidence = evidence
                return z
        z = StaticZone(cx, t, evidence)
        self.zones.append(z)
        return z

    def _expire(self, t):
        self.history = [s for s in self.history if t - s.t <= HISTORY_S]
        self.zones = [z for z in self.zones if t - z.at <= ZONE_TTL_S]

    # --- using -----------------------------------------------------------
    def is_static(self, cx, t=None):
        """True when this image position is known not to move with the camera."""
        t = self.clock() if t is None else float(t)
        self._expire(t)
        return any(abs(z.cx - float(cx)) <= SAME_PX for z in self.zones)

    def accept(self, cx, t=None):
        """is_static inverted, counting what it throws away. Use this in a loop."""
        if self.is_static(cx, t=t):
            self.rejected += 1
            return False
        return True

    def describe(self):
        if not self.zones:
            return "no static zones learned"
        return "; ".join(repr(z) for z in self.zones)
