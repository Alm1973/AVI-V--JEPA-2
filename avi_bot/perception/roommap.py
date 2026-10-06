"""Where things are, in the only coordinates this robot can act on.

AVI is bolted to a desk. It cannot walk, so it never needs to know where
anything is in metres -- it needs to know WHICH WAY TO TURN to look at it.
That makes the useful map spherical rather than Cartesian: a direction, stored
as the (base_yaw, wrist) the arm would hold to put a thing in the centre of
frame.

WHY THIS IS EASY HERE AND HARD ELSEWHERE
Visual mapping is normally hard because the camera's pose has to be estimated
from the pictures. Here it is simply known: the arm reports its own joint
angles, so every frame arrives already labelled with where the camera was
pointing. Structure from KNOWN motion, not structure from motion.

WHAT IS DELIBERATELY NOT ATTEMPTED
True 3D triangulation. The camera rides about 200 mm of links, so panning gives
a stereo baseline of centimetres against objects metres away -- the depth error
would exceed the depth. Apparent box size gives a better distance cue per
object over time (the same mug, bigger, is nearer) and that is recorded for
later rather than converted into a false metre figure now.

THE UNITS ARE COMMANDED DEGREES, NOT REAL ONES. base_yaw under-travels its
command by roughly a third (see config/joints.toml), so these are not angles
anyone could measure with a protractor. They are exactly the right units
anyway, because the only thing done with them is to command the same joints
back to the same place.
"""
import math
import time

# A sighting this far from an existing one is the same object. In degrees of
# commanded yaw: about a quarter of a frame width at 83.9 deg/frame, which is
# generous enough to survive box jitter and tight enough to tell a mug from
# the laptop beside it.
SAME_DEG = 10.0

# Half-life for a sighting's weight. A mug moves; a monitor does not. Rather
# than guess which, every sighting decays and a thing that keeps being seen
# in one place keeps a strong position while a thing that moved fades from
# where it used to be.
HALF_LIFE_S = 6 * 3600.0

# Below this total weight an entry is not worth reporting.
MIN_WEIGHT = 0.35


class Place:
    """One object's believed direction, as an angle pair."""

    __slots__ = ("label", "yaw", "wrist", "weight", "first_at", "last_at",
                 "count", "best_conf", "px_size")

    def __init__(self, label, yaw, wrist, at, conf=0.0, px_size=None):
        self.label = label
        self.yaw = float(yaw)
        self.wrist = float(wrist)
        self.weight = 1.0
        self.first_at = float(at)
        self.last_at = float(at)
        self.count = 1
        self.best_conf = float(conf)
        self.px_size = px_size

    def decayed(self, now):
        age = max(0.0, now - self.last_at)
        return self.weight * (0.5 ** (age / HALF_LIFE_S))

    def __repr__(self):
        return (f"<Place {self.label} yaw={self.yaw:.1f} wrist={self.wrist:.1f} "
                f"seen {self.count}x conf<={self.best_conf:.2f}>")


class RoomMap:
    def __init__(self, yaw_deg_per_frame=83.9, wrist_deg_per_frame=32.2,
                 frame=(1280, 720), clock=time.time):
        self.yaw_deg_per_frame = float(yaw_deg_per_frame)
        self.wrist_deg_per_frame = float(wrist_deg_per_frame)
        self.frame = frame
        self.clock = clock
        self.places = []
        self.anchors = {}          # taught pose name -> (yaw, wrist)

    # --- geometry --------------------------------------------------------
    def direction_of(self, cx, cy, yaw, wrist):
        """A detection at (cx, cy), seen from (yaw, wrist), points where?

        Inverts the same two measured constants the tracking loop uses, with
        the same signs: raising yaw moves a landmark LEFT, so something right
        of centre lies at a HIGHER yaw; raising wrist moves a landmark DOWN,
        so something above centre lies at a LOWER wrist.
        """
        w, h = self.frame
        ox = (float(cx) - w / 2.0) / w
        oy = (float(cy) - h / 2.0) / h
        return (float(yaw) + ox * self.yaw_deg_per_frame,
                float(wrist) + oy * self.wrist_deg_per_frame)

    @staticmethod
    def _gap(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    # --- building --------------------------------------------------------
    def observe(self, label, cx, cy, yaw, wrist, conf=0.0, px_size=None,
                at=None):
        """Record one detection. Returns the Place it landed in."""
        at = self.clock() if at is None else float(at)
        want = self.direction_of(cx, cy, yaw, wrist)
        for place in self.places:
            if place.label != label:
                continue
            if self._gap((place.yaw, place.wrist), want) > SAME_DEG:
                continue
            # Weighted mean, so a thing seen fifty times is not dragged
            # across the room by one bad box.
            old = place.decayed(at)
            total = old + 1.0
            place.yaw = (place.yaw * old + want[0]) / total
            place.wrist = (place.wrist * old + want[1]) / total
            place.weight = total
            place.last_at = at
            place.count += 1
            place.best_conf = max(place.best_conf, float(conf))
            if px_size:
                place.px_size = px_size
            return place
        place = Place(label, want[0], want[1], at, conf=conf, px_size=px_size)
        self.places.append(place)
        return place

    def anchor(self, name, yaw, wrist):
        """Tie the map to a pose he named, so it can be described in his terms."""
        self.anchors[name] = (float(yaw), float(wrist))

    # --- using -----------------------------------------------------------
    def where_is(self, label, now=None):
        """The best believed direction for a label, or None.

        Returns (yaw, wrist, confidence_in_the_position).
        """
        now = self.clock() if now is None else now
        best, best_w = None, 0.0
        for place in self.places:
            if place.label != label:
                continue
            w = place.decayed(now)
            if w > best_w:
                best, best_w = place, w
        if best is None or best_w < MIN_WEIGHT:
            return None
        return best.yaw, best.wrist, best_w

    def nearest_anchor(self, yaw, wrist):
        """Which taught pose a direction is closest to, for describing it.

        'Near where TALL looks' is a sentence he can act on; '61.4 degrees of
        commanded yaw' is not.
        """
        if not self.anchors:
            return None, None
        name = min(self.anchors,
                   key=lambda n: self._gap(self.anchors[n], (yaw, wrist)))
        return name, self._gap(self.anchors[name], (yaw, wrist))

    def known(self, now=None):
        """Everything worth reporting, strongest first."""
        now = self.clock() if now is None else now
        live = [(p, p.decayed(now)) for p in self.places]
        live = [(p, w) for p, w in live if w >= MIN_WEIGHT]
        live.sort(key=lambda pw: -pw[1])
        return live

    def forget_stale(self, now=None):
        """Drop what has decayed past usefulness. Keeps the map small."""
        now = self.clock() if now is None else now
        before = len(self.places)
        self.places = [p for p in self.places if p.decayed(now) >= MIN_WEIGHT]
        return before - len(self.places)

    # --- where to look next ---------------------------------------------
    def survey_stops(self, yaw_limits, wrist_levels, overlap=0.25):
        """Joint positions that between them see the whole reachable view.

        Steps by a frame width minus an overlap, because a survey that steps
        by exactly one frame leaves seams, and anything sitting on a seam is
        never mapped at all.
        """
        lo, hi = yaw_limits
        step = self.yaw_deg_per_frame * (1.0 - overlap)
        stops = []
        for wrist in wrist_levels:
            yaw = lo + step / 2.0
            row = []
            while yaw <= hi:
                row.append((round(yaw, 1), round(float(wrist), 1)))
                yaw += step
            # Alternate direction per row: a boustrophedon sweep, so the arm
            # does not travel the whole width back to the start between rows.
            stops.extend(row if len(stops) // max(1, len(row)) % 2 == 0
                         else list(reversed(row)))
        return stops

    def unexplored(self, yaw_limits, wrist_levels, now=None):
        """Survey stops with nothing mapped near them yet.

        This is what makes 'look around' purposeful rather than random: the
        gaze goes where the map is blank.
        """
        now = self.clock() if now is None else now
        seen = [(p.yaw, p.wrist) for p, _ in self.known(now)]
        out = []
        for stop in self.survey_stops(yaw_limits, wrist_levels):
            if not any(self._gap(stop, s) <= self.yaw_deg_per_frame / 2.0
                       for s in seen):
                out.append(stop)
        return out

    # --- persistence -----------------------------------------------------
    def to_dict(self):
        return {"yaw_deg_per_frame": self.yaw_deg_per_frame,
                "wrist_deg_per_frame": self.wrist_deg_per_frame,
                "frame": list(self.frame),
                "anchors": {k: list(v) for k, v in self.anchors.items()},
                "places": [{"label": p.label, "yaw": round(p.yaw, 2),
                            "wrist": round(p.wrist, 2),
                            "weight": round(p.weight, 3),
                            "first_at": p.first_at, "last_at": p.last_at,
                            "count": p.count,
                            "best_conf": round(p.best_conf, 3),
                            "px_size": p.px_size}
                           for p in self.places]}

    @classmethod
    def from_dict(cls, data, clock=time.time):
        m = cls(yaw_deg_per_frame=data.get("yaw_deg_per_frame", 83.9),
                wrist_deg_per_frame=data.get("wrist_deg_per_frame", 32.2),
                frame=tuple(data.get("frame", (1280, 720))), clock=clock)
        m.anchors = {k: tuple(v) for k, v in (data.get("anchors") or {}).items()}
        for row in data.get("places") or []:
            place = Place(row["label"], row["yaw"], row["wrist"],
                          row.get("first_at", 0.0),
                          conf=row.get("best_conf", 0.0),
                          px_size=row.get("px_size"))
            place.weight = row.get("weight", 1.0)
            place.last_at = row.get("last_at", place.first_at)
            place.count = row.get("count", 1)
            m.places.append(place)
        return m
