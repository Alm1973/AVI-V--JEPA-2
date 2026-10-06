"""Tell a person in the room from the robot's own hardware in shot.

The camera is bolted to the arm, so anything else bolted to the arm sits at a
FIXED position in the image however far the arm swings, while a real object in
the room slides across the frame by `d_yaw / yaw_deg_per_frame` of its width.
That difference is worth a factor of thirty where confidence is worth nothing:
measured 2026-10-05, Shaurya scored 0.75-0.89 and the arm's own hardware
scored 0.41-0.72, two ranges a threshold cannot separate -- at 0.70 the
tracker locked onto itself and saturated, at 0.75 it found nothing in 84
frames.

WHY THIS IS THE SECOND DESIGN
The first version blacklisted image REGIONS: positions where something had
been seen to not move. It demonstrably worked -- it caught the hardware at
x = 1109, 1109, 1109, zero drift across a 30 degree pan that should have moved
it 458 px -- and it still had to be thrown away, because a later run learned a
confirmed zone at x = 632 and HE SITS AT x = 618..639. The arm's own structure
overlaps his seat in the image, so any filter that rejects regions will
eventually reject him. No amount of threshold tuning fixes that; the unit of
judgement was simply wrong.

So the verdict now attaches to a TRACK, never to a position, and the test is
POSITIVE: a candidate is believed to be a real object because it was seen to
move as the pan predicts, rather than being disbelieved for sitting somewhere
suspicious.

HOW THE MATCHING ITSELF DISCRIMINATES
Given a pan of d_yaw since a track was last seen, there are exactly two places
its next sighting can be:

    real in the room  ->  last_cx - d_yaw * px_per_deg
    bolted to the arm ->  last_cx

Whichever the new detection is closer to is both the match AND the verdict.
One comparison does both jobs, and when the two predictions are too close
together to tell apart -- a small pan -- the honest answer is no verdict,
which is what gets returned.
"""
import itertools
import time

# Matching tolerance. Detection boxes jitter by this sort of amount between
# frames even when nothing moves.
MATCH_PX = 70.0

# The two hypotheses have to be at least this far apart before the match can
# mean anything, or a detection would be assigned to whichever happened to be
# marginally nearer by chance. Two tolerances, so neither hypothesis can be
# claimed by a detection that fits both.
MIN_SEPARATION_PX = 2 * MATCH_PX

# Votes a track needs before its verdict is acted on. One pairing is not
# enough: in the region-based version a single marginal pairing was what
# blacklisted his seat.
VOTES_NEEDED = 2

# A track with no sighting for this long is forgotten.
TRACK_TTL_S = 8.0

# Verdicts.
REAL = "real"             # moved with the pan: something in the room
ATTACHED = "attached"     # did not move: part of the arm
UNKNOWN = "unknown"       # not enough pan to say


# How much to trust a candidate parallax could not judge, as a function of how
# well lit the frame is. MEASURED 2026-10-06, same four poses, empty room:
#
#   dark  (frame mean ~30)   worst false 'person'  0.61-0.72
#   light (frame mean 120+)  worst false 'person'  0.31
#
# and Shaurya himself reads 0.75-0.90 in both. So the overlap that caused all
# of yesterday's trouble -- a 0.75 floor finding nothing, a 0.70 floor locking
# onto the arm -- is substantially a LOW-LIGHT artifact. In daylight there is a
# clean gap and a lower threshold is both safe and much more sensitive.
#
# A fixed threshold therefore has to be wrong somewhere: tuned for the dark it
# misses him in daylight, tuned for daylight it hallucinates him at night.
TRUST_BY_LIGHT = (
    (80.0, 0.65),    # frame mean above this: well lit, worst false ~0.31
    (50.0, 0.75),    # dim
    (0.0, 0.85),     # dark: only a strong detection, or parallax, is believable
)


def trust_for_light(mean_level):
    """The confidence an unjudged candidate needs, given frame brightness.

    None (brightness unknown) gets the strictest value, because an unmeasured
    frame is not evidence of a well-lit one.
    """
    if mean_level is None:
        return TRUST_BY_LIGHT[-1][1]
    for floor, trust in TRUST_BY_LIGHT:
        if float(mean_level) >= floor:
            return trust
    return TRUST_BY_LIGHT[-1][1]


class Track:
    __slots__ = ("id", "cx", "yaw", "at", "votes", "note", "seen")

    def __init__(self, tid, cx, yaw, at):
        self.id = tid
        self.cx = float(cx)
        self.yaw = float(yaw)
        self.at = float(at)
        self.votes = {REAL: 0, ATTACHED: 0}
        self.note = None
        self.seen = 1

    @property
    def verdict(self):
        for name in (REAL, ATTACHED):
            if self.votes[name] >= VOTES_NEEDED and \
                    self.votes[name] > self.votes[other(name)]:
                return name
        return UNKNOWN

    def __repr__(self):
        return (f"<Track {self.id} x={self.cx:.0f} {self.verdict} "
                f"real={self.votes[REAL]} attached={self.votes[ATTACHED]}"
                f"{' ' + self.note if self.note else ''}>")


def other(name):
    return ATTACHED if name == REAL else REAL


class ParallaxTracker:
    def __init__(self, yaw_deg_per_frame, clock=time.time):
        self.yaw_deg_per_frame = float(yaw_deg_per_frame)
        self.clock = clock
        self.tracks = []
        self._ids = itertools.count(1)
        self.rejected = 0
        self.accepted = 0

    # --- geometry --------------------------------------------------------
    def px_per_deg(self, width):
        if not self.yaw_deg_per_frame:
            return 0.0
        return float(width) / self.yaw_deg_per_frame

    def predictions(self, track, yaw, width):
        """Where this track should appear, under each hypothesis."""
        d_yaw = float(yaw) - track.yaw
        shift = d_yaw * self.px_per_deg(width)
        # Measured sign: raising base_yaw moves a fixed landmark LEFT, so a
        # real object's cx falls as yaw rises.
        return {REAL: track.cx - shift, ATTACHED: track.cx}, abs(shift)

    # --- the one call the caller needs -----------------------------------
    def classify(self, detections, yaw, width, t=None):
        """Attach a verdict to each detection. Does not drop anything.

        `detections` is a list of dicts with a "cx". Each gets "parallax" (one
        of REAL / ATTACHED / UNKNOWN) and "track" added in place, and the list
        is returned for convenience.
        """
        t = self.clock() if t is None else float(t)
        self._expire(t)
        taken = set()
        for det in sorted(detections, key=lambda d: -d.get("conf", 0.0)):
            cx = float(det["cx"])
            best = None
            for track in self.tracks:
                if track.id in taken:
                    continue
                preds, sep = self.predictions(track, yaw, width)
                for name, want in preds.items():
                    gap = abs(cx - want)
                    if gap > MATCH_PX:
                        continue
                    # A small pan leaves the two predictions on top of each
                    # other, so a match cannot be read as evidence either way.
                    verdict = name if sep >= MIN_SEPARATION_PX else UNKNOWN
                    if best is None or gap < best[0]:
                        best = (gap, track, verdict, sep, want)
            if best is None:
                track = Track(next(self._ids), cx, yaw, t)
                self.tracks.append(track)
                det["parallax"] = UNKNOWN
                det["track"] = track.id
                continue
            gap, track, verdict, sep, want = best
            taken.add(track.id)
            if verdict != UNKNOWN:
                track.votes[verdict] += 1
                track.note = (f"moved {cx - track.cx:+.0f} px of "
                              f"{-(want - track.cx):+.0f} predicted")
            track.cx = cx
            track.yaw = float(yaw)
            track.at = t
            track.seen += 1
            det["parallax"] = track.verdict
            det["track"] = track.id
        return detections

    def accept(self, det, min_conf_unknown=0.75):
        """Should this detection be believed?

        ATTACHED is rejected outright -- it is the arm. REAL is accepted. And
        UNKNOWN, which is the common case because the arm is usually not
        panning, falls back to confidence: that is the only evidence available,
        and refusing everything unproven would mean the robot could not see
        him while holding still.
        """
        verdict = det.get("parallax", UNKNOWN)
        if verdict == ATTACHED:
            self.rejected += 1
            return False
        if verdict == REAL:
            self.accepted += 1
            return True
        ok = float(det.get("conf", 0.0)) >= min_conf_unknown
        self.accepted += int(ok)
        self.rejected += int(not ok)
        return ok

    def _expire(self, t):
        self.tracks = [k for k in self.tracks if t - k.at <= TRACK_TTL_S]

    def describe(self):
        if not self.tracks:
            return "no tracks"
        return "; ".join(repr(k) for k in self.tracks)

    def attached_tracks(self):
        return [k for k in self.tracks if k.verdict == ATTACHED]
