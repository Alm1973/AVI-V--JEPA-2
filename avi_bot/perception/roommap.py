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

A MAP IS ONLY VALID FOR ONE SHOULDER AND ELBOW. The camera rides the end of a
three-link arm, so where it POINTS depends on shoulder and elbow as well as on
wrist -- the same (yaw, wrist) aims somewhere different if the shoulder has
moved. So the map records the configuration it was built at and refuses to
mix. Surveying across the taught poses would have silently violated this,
since each one sets all four joints.

THE UNITS ARE COMMANDED DEGREES, NOT REAL ONES. base_yaw under-travels its
command by roughly a third (see config/joints.toml), so these are not angles
anyone could measure with a protractor. They are exactly the right units
anyway, because the only thing done with them is to command the same joints
back to the same place.
"""
import math
import time

from avi_bot.perception import world as world_mod

# A sighting this far from an existing one is the same object. In degrees of
# commanded yaw: about a quarter of a frame width at 83.9 deg/frame, which is
# generous enough to survive box jitter and tight enough to tell a mug from
# the laptop beside it.
#
# The detector's vocabulary has near-synonyms it cannot reliably tell apart,
# and one object split across two of them in a real survey: a single stop
# logged "book:0.43, notebook:0.35, book:0.43, notebook" for the same thing.
# The map files both under one name rather than pretending to a distinction the
# instrument cannot make. cup/mug is the other pair in settings.DESK_OBJECTS.
LABEL_ALIASES = {"cup": "mug", "notebook": "book"}


def canonical_label(label):
    label = " ".join((label or "").split()).lower()
    return LABEL_ALIASES.get(label, label)


# A FLOOR, not the whole rule. See same_deg(): a fixed threshold mapped one
# laptop as two places 20 degrees apart, because a large object's box centre
# sits near the middle of the frame across several tilt rows at once and each
# row then claims it at that row's own elevation. A point landmark slides out
# of frame; a laptop does not.
SAME_DEG = 10.0

# How much of an object's own angular width counts as "the same place". You
# cannot localise a thing more precisely than its own extent, so a laptop
# spanning half the frame has a direction good to tens of degrees and no
# better. 1.0 means two sightings closer than the object is wide are one
# object.
SAME_SIZE_FRACTION = 1.0

# How fast a place is forgotten. A mug moves; a monitor does not. Rather than
# guess which, every sighting decays and a thing that keeps being seen in one
# place keeps a strong position while a thing that moved fades from where it
# used to be.
#
# The half-life is NOT fixed, and this is the whole design. Under a single
# flat half-life the lifetime of a place grows only as log2(weight), so each
# DOUBLING of the evidence buys one more half-life -- about six hours. Nothing
# can ever become permanent, however long it has sat there. Worse, weight
# counts frames, and frames measure how long the camera happened to stare: a
# mug in view for ten minutes at 5 fps outscores a monitor seen briefly on
# thirty separate days, which is backwards.
#
# So durability is earned by SPACING instead (Ebbinghaus): each visit that is
# genuinely separated in time multiplies the half-life. Something glimpsed
# once fades overnight; something still in the same spot a week later is
# furniture, and the map should treat it that way.
BASE_HALF_LIFE_S = 12 * 3600.0

# Sightings closer together than this are the same visit. Without a gap rule a
# 5 fps logger would bank thousands of "visits" in a minute and call a passing
# mug permanent.
VISIT_GAP_S = 4 * 3600.0

# What each extra visit multiplies the half-life by.
SPACING = 3.0

# Ceiling on a place's accumulated weight. Weight is the mass of the position
# average, and an uncapped one has two bad effects: a thing seen a thousand
# times can never be seen to MOVE, because a thousand stale frames outvote
# every new one, and frame count leaks back into durability through the decay.
# Capping it keeps the position responsive and keeps a long stare from being
# mistaken for a long acquaintance.
#
# The value is set by the outlier it has to survive: one bad box moves the
# average by its distance over MAX_WEIGHT+1, so 20 keeps a single wild
# detection under half a degree while a dozen consistent sightings at a new
# spot still carry the position most of the way there.
MAX_WEIGHT = 20.0

# Nothing is believed forever without being seen again. A year of silence is
# long enough that the room has certainly changed.
MAX_HALF_LIFE_S = 365 * 24 * 3600.0

# Below this total weight an entry is not worth reporting.
MIN_WEIGHT = 0.35

# Distinct VIEWPOINTS a place needs before it is believed. Not sightings:
# the first survey took two frames per stop and every place came back "seen
# 2x", which looked like confirmation and was the same picture twice. Two
# frames from one pose agree about a hallucination as readily as about a mug.
MIN_STOPS = 2

# A place whose direction falls outside the arm's reach is still a true fact
# about the room and a useless instruction, so it is kept and flagged rather
# than silently clamped -- commanding it would be clamped by the limiter and
# the robot would stare confidently at the wrong thing.
class Unreachable(Exception):
    pass


class Place:
    """One object's believed direction, as an angle pair."""

    __slots__ = ("label", "yaw", "wrist", "weight", "first_at", "last_at",
                 "count", "best_conf", "px_size", "stops", "visits",
                 "last_visit_at")

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
        self.stops = set()
        self.visits = 1
        self.last_visit_at = float(at)

    @property
    def half_life(self):
        """How long this place keeps half its weight, earned by revisiting.

        One visit is the base. Every separated visit after that multiplies it,
        so belief hardens with time rather than with frame count.
        """
        return min(MAX_HALF_LIFE_S,
                   BASE_HALF_LIFE_S * (SPACING ** (self.visits - 1)))

    def decayed(self, now):
        age = max(0.0, now - self.last_at)
        return self.weight * (0.5 ** (age / self.half_life))

    def note_visit(self, at):
        """Count a separated return. Returns True if this began a new visit."""
        if at - self.last_visit_at < VISIT_GAP_S:
            return False
        self.visits += 1
        self.last_visit_at = float(at)
        return True

    @property
    def confirmed(self):
        """Seen from at least MIN_STOPS different viewpoints."""
        return len(self.stops) >= MIN_STOPS

    def __repr__(self):
        return (f"<Place {self.label} yaw={self.yaw:.1f} wrist={self.wrist:.1f} "
                f"seen {self.count}x from {len(self.stops)} views "
                f"over {self.visits} visits "
                f"conf<={self.best_conf:.2f}"
                f"{'' if self.confirmed else ' UNCONFIRMED'}>")


class RoomMap:
    # How far shoulder or elbow may differ before the map no longer applies.
    # Tight, because a few degrees at the shoulder swings the camera's aim by
    # much more than a few degrees at the wrist.
    CONFIG_TOLERANCE_DEG = 3.0

    def __init__(self, yaw_deg_per_frame=83.9, wrist_deg_per_frame=32.2,
                 frame=(1280, 720), clock=time.time, shoulder=None,
                 elbow=None, view=None):
        # A world.View when level is measured: directions are then worked out
        # on the sphere instead of with one constant per axis. See
        # direction_of. None keeps the flat constants.
        self.view = view
        self.yaw_deg_per_frame = float(yaw_deg_per_frame)
        self.wrist_deg_per_frame = float(wrist_deg_per_frame)
        self.frame = frame
        self.clock = clock
        self.places = []
        self.anchors = {}          # taught pose name -> (yaw, wrist)
        # The arm configuration this map describes. None until the first
        # observation fixes it.
        self.shoulder = shoulder
        self.elbow = elbow

    # --- geometry --------------------------------------------------------
    def same_deg(self, *px_sizes):
        """How close two sightings must be to be one object, in degrees.

        Scales with the object's apparent size. A fixed 10 degrees split one
        laptop across two tilt rows 20 degrees apart: a big box's centre stays
        near the middle of the frame however the camera tilts, so each row
        files it at its own elevation and the map grows a copy per row.
        """
        got = [float(p) for p in px_sizes if p]
        if not got:
            return SAME_DEG
        if self.view is not None:
            span = math.degrees(max(got) / self.view.focal) / self.view.yaw_gain
        else:
            span = max(got) / float(self.frame[0]) * self.yaw_deg_per_frame
        return max(SAME_DEG, SAME_SIZE_FRACTION * span)

    @property
    def geometry(self):
        return "world" if self.view is not None else "flat"

    def direction_of(self, cx, cy, yaw, wrist):
        """A detection at (cx, cy), seen from (yaw, wrist), points where?

        Returned as the (yaw, wrist) that would put it at frame centre.

        WITH A VIEW (level measured): the pixel's ray is found on the sphere
        and turned back into the joint angles that centre it. On the 2026-10-06
        dense survey this put every object in ONE cluster at any threshold
        and cut the scatter from 3.19 to 2.37 deg; the flat constants split
        the laptop in two at 8 deg and scattered the mug by 6.

        Inverts the same two measured constants the tracking loop uses, with
        the same signs: raising yaw moves a landmark LEFT, so something right
        of centre lies at a HIGHER yaw; raising wrist moves a landmark DOWN,
        so something above centre lies at a LOWER wrist.
        """
        if self.view is not None:
            return self.view.aim(*self.view.direction(cx, cy, yaw, wrist))
        w, h = self.frame
        ox = (float(cx) - w / 2.0) / w
        oy = (float(cy) - h / 2.0) / h
        return (float(yaw) + ox * self.yaw_deg_per_frame,
                # MINUS. Raising the wrist moves a fixed landmark DOWN the
                # frame (measured 2026-10-06: wrist 151.3/157.3/163.3 put a
                # laptop at cy 344/516/599, +21.3 px/deg), so something BELOW
                # centre lies at a LOWER wrist. This was `+` until then: every
                # map entry was placed at 2*row - truth, so one object seen
                # from survey rows 9.4 degrees apart landed ~19 degrees apart
                # -- the "duplicate laptop" blamed first on a constant and
                # then on box size. The tracking loop had it right all along
                # (test_attention: "below centre needs LESS wrist").
                float(wrist) - oy * self.wrist_deg_per_frame)

    @staticmethod
    def _gap(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    # --- building --------------------------------------------------------
    def valid_for(self, shoulder, elbow):
        """Does this map describe the arm's current shape?"""
        if self.shoulder is None or shoulder is None:
            return True            # nothing recorded yet, or not told
        return (abs(self.shoulder - float(shoulder))
                <= self.CONFIG_TOLERANCE_DEG
                and abs(self.elbow - float(elbow))
                <= self.CONFIG_TOLERANCE_DEG)

    def observe(self, label, cx, cy, yaw, wrist, conf=0.0, px_size=None,
                at=None, shoulder=None, elbow=None):
        """Record one detection. Returns the Place it landed in.

        Raises if the arm's shoulder or elbow has moved away from the shape
        this map was built at, because the same (yaw, wrist) then aims
        somewhere else entirely and the sighting would be filed under a
        direction the robot cannot reproduce.
        """
        at = self.clock() if at is None else float(at)
        label = canonical_label(label)
        if shoulder is not None:
            if self.shoulder is None:
                self.shoulder, self.elbow = float(shoulder), float(elbow)
            elif not self.valid_for(shoulder, elbow):
                raise Unreachable(
                    f"map was built at shoulder {self.shoulder:.1f} elbow "
                    f"{self.elbow:.1f}; the arm is at {shoulder:.1f}/"
                    f"{elbow:.1f}, so the same yaw and wrist point elsewhere")
        viewpoint = (round(float(yaw), 1), round(float(wrist), 1))
        want = self.direction_of(cx, cy, yaw, wrist)
        for place in self.places:
            if place.label != label:
                continue
            if self._gap((place.yaw, place.wrist), want) > self.same_deg(
                    place.px_size, px_size):
                continue
            # Weighted mean, so a thing seen fifty times is not dragged
            # across the room by one bad box.
            old = place.decayed(at)
            total = old + 1.0
            place.yaw = (place.yaw * old + want[0]) / total
            place.wrist = (place.wrist * old + want[1]) / total
            place.weight = min(MAX_WEIGHT, total)
            place.last_at = at
            place.count += 1
            place.stops.add(viewpoint)
            place.note_visit(at)
            place.best_conf = max(place.best_conf, float(conf))
            if px_size:
                place.px_size = px_size
            return place
        place = Place(label, want[0], want[1], at, conf=conf, px_size=px_size)
        place.stops.add(viewpoint)
        self.places.append(place)
        return place

    # How close the aim a look used must be to a place for that place to be
    # the one it was looking for (degrees). Aims come from where_is(), so
    # this is rounding, not judgement.
    WAS_HERE_DEG = 3.0

    def correct(self, label, cx, cy, yaw, wrist, was, conf=0.0,
                px_size=None, at=None, shoulder=None, elbow=None):
        """A deliberate look at `was` = (yaw, wrist), where where_is()
        pointed, did not show `label`; a stop near it did, at pixel (cx, cy)
        from (yaw, wrist). Move the place there.

        observe() would average the sighting in -- right for a jittery box,
        wrong for this: the look PROVED the place is not at the old
        direction. On the robot the mug was found 15 degrees off its place
        look after look, and a place with weight 17 moves under a degree
        per sighting (2026-10-08). The place keeps its weight and history:
        it is the same object, believed in the wrong direction. A second
        place of the label already at the new direction is merged in.
        """
        at = self.clock() if at is None else float(at)
        label = canonical_label(label)
        if (shoulder is not None and self.shoulder is not None
                and not self.valid_for(shoulder, elbow)):
            raise Unreachable(
                f"map was built at shoulder {self.shoulder:.1f} elbow "
                f"{self.elbow:.1f}; the arm is at {shoulder:.1f}/"
                f"{elbow:.1f}")
        mine = [p for p in self.places if p.label == label]
        old = min(mine, key=lambda p: self._gap((p.yaw, p.wrist), was),
                  default=None)
        if old is None or self._gap((old.yaw, old.wrist),
                                    was) > self.WAS_HERE_DEG:
            return self.observe(label, cx, cy, yaw, wrist, conf=conf,
                                px_size=px_size, at=at, shoulder=shoulder,
                                elbow=elbow)
        want = self.direction_of(cx, cy, yaw, wrist)
        old.yaw, old.wrist = float(want[0]), float(want[1])
        old.last_at = at
        old.count += 1
        old.stops.add((round(float(yaw), 1), round(float(wrist), 1)))
        old.note_visit(at)
        old.best_conf = max(old.best_conf, float(conf))
        if px_size:
            old.px_size = px_size
        for p in list(self.places):
            if (p is not old and p.label == label
                    and self._gap((p.yaw, p.wrist), want)
                    <= self.same_deg(p.px_size, px_size)):
                old.weight = min(MAX_WEIGHT, old.weight + p.decayed(at))
                old.stops |= p.stops
                old.count += p.count
                self.places.remove(p)
        return old

    def anchor(self, name, yaw, wrist):
        """Tie the map to a pose he named, so it can be described in his terms."""
        self.anchors[name] = (float(yaw), float(wrist))

    # --- using -----------------------------------------------------------
    def last_seen(self, label, now=None):
        """When the place where_is() would answer with was last seen, or
        None. A confirmed place is a verified, multi-view sighting, so this
        is a real "I saw it at", not a guess."""
        now = self.clock() if now is None else now
        label = canonical_label(label)
        best, best_w = None, 0.0
        for place in self.places:
            if place.label != label or not place.confirmed:
                continue
            w = place.decayed(now)
            if w > best_w:
                best, best_w = place, w
        if best is None or best_w < MIN_WEIGHT:
            return None
        return best.last_at

    def where_is(self, label, now=None):
        """The best believed direction for a label, or None.

        Returns (yaw, wrist, confidence_in_the_position).
        """
        now = self.clock() if now is None else now
        label = canonical_label(label)
        best, best_w = None, 0.0
        for place in self.places:
            if place.label != label or not place.confirmed:
                continue
            w = place.decayed(now)
            if w > best_w:
                best, best_w = place, w
        if best is None or best_w < MIN_WEIGHT:
            return None
        return best.yaw, best.wrist, best_w

    def reachable(self, yaw, wrist, yaw_limits, wrist_limits):
        """Can the arm actually point there?"""
        return (yaw_limits[0] <= yaw <= yaw_limits[1]
                and wrist_limits[0] <= wrist <= wrist_limits[1])

    def look_at(self, label, yaw_limits, wrist_limits, now=None):
        """Where to point to see `label`. Raises Unreachable if it is outside.

        Deliberately an exception rather than a clamped value: a clamped
        command makes the robot stare confidently at the wrong thing, and the
        caller should say "it is further round than I can turn" instead.
        """
        got = self.where_is(label, now=now)
        if got is None:
            return None
        yaw, wrist, weight = got
        if not self.reachable(yaw, wrist, yaw_limits, wrist_limits):
            raise Unreachable(
                f"{label} lies at yaw {yaw:.1f} wrist {wrist:.1f}, outside "
                f"yaw {yaw_limits} wrist {wrist_limits}")
        return yaw, wrist, weight

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
        live = [(p, w) for p, w in live
                if w >= MIN_WEIGHT and p.confirmed]
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
                "geometry": self.geometry,
                "view": self.view.to_dict() if self.view is not None else None,
                "frame": list(self.frame),
                "shoulder": self.shoulder, "elbow": self.elbow,
                "anchors": {k: list(v) for k, v in self.anchors.items()},
                "places": [{"label": p.label, "yaw": round(p.yaw, 2),
                            "wrist": round(p.wrist, 2),
                            "weight": round(p.weight, 3),
                            "first_at": p.first_at, "last_at": p.last_at,
                            "count": p.count,
                            "best_conf": round(p.best_conf, 3),
                            "px_size": p.px_size,
                            "visits": p.visits,
                            "last_visit_at": p.last_visit_at,
                            "stops": [list(v) for v in sorted(p.stops)]}
                           for p in self.places]}

    @classmethod
    def from_dict(cls, data, clock=time.time):
        m = cls(yaw_deg_per_frame=data.get("yaw_deg_per_frame", 83.9),
                wrist_deg_per_frame=data.get("wrist_deg_per_frame", 32.2),
                frame=tuple(data.get("frame", (1280, 720))), clock=clock,
                shoulder=data.get("shoulder"), elbow=data.get("elbow"),
                view=(world_mod.View(**data["view"]) if data.get("view")
                      else None))
        m.anchors = {k: tuple(v) for k, v in (data.get("anchors") or {}).items()}
        for row in data.get("places") or []:
            place = Place(row["label"], row["yaw"], row["wrist"],
                          row.get("first_at", 0.0),
                          conf=row.get("best_conf", 0.0),
                          px_size=row.get("px_size"))
            place.weight = row.get("weight", 1.0)
            place.last_at = row.get("last_at", place.first_at)
            place.count = row.get("count", 1)
            place.stops = {tuple(v) for v in (row.get("stops") or [])}
            place.visits = int(row.get("visits", 1))
            place.last_visit_at = float(
                row.get("last_visit_at", place.last_at))
            m.places.append(place)
        return m
