"""Range from the arm's own motion. Structure from KNOWN motion.

AVI is bolted to a desk and has one camera, so depth looks impossible. It is
not, and the reason is that the camera is not on the rotation axis. When
base_yaw turns, the camera both ROTATES and TRANSLATES: it swings along an arc
of radius r, where r is its horizontal offset from the base axis. That arc is a
stereo baseline, and unlike a handheld camera's, its length is known from the
joint angles the firmware already reports.

Working out the apparent motion. Put the base axis at the origin; at actual
joint angle `th` the camera sits at r*(cos(th+phi), sin(th+phi)) and looks
along bearing th+psi. For a point at camera-distance `rho`, the image angle `a`
measured from the optical axis moves at

    da/dth = -1 - (r/rho) * cos(eta)

where eta is the angle between the camera-to-point direction and the camera's
own radial direction. The -1 is pure rotation: the whole frame sweeps against
the turn, the same for everything, carrying no depth. The second term is
parallax, and it is the entire signal -- it scales as r/rho, so a point at
rho = r appears to move twice as fast as the far wall.

Inverted, with `s` the measured image-angle shift per COMMANDED degree and `g`
the commanded-to-actual gain of the joint:

    rho = r * cos(eta) / (s/g - 1)

THIS IS A CORRECTION TO THE EXISTING CALIBRATION, not just an addition.
`yaw_deg_per_frame = 83.9` was measured as one number off one landmark and
attributed entirely to base_yaw under-travelling its command (0.62x). But
s = g * (1 + (r/rho) cos eta) has two unknowns in it, and a single landmark
cannot separate them: the 0.62 is some mixture of servo under-travel and that
landmark's parallax. Every object at a different distance has a different
effective deg-per-frame. The global constant is the value for whatever was in
frame the day it was measured.

WHAT IS AND IS NOT IDENTIFIABLE. Count it honestly: s_i = g*(1 + c_i) with
c_i = (r/rho_i) cos a_i gives n measurements and n+1 unknowns (g, and one c
per track). Underdetermined by exactly one, FOR ANY NUMBER OF TRACKS. More
objects in the pan never recover the gain.

An earlier version of this file claimed the ratio s_i/s_j cancels g and
therefore gives relative depth for free. It does not. g does cancel in the
ratio of RATES, but recovering a range needs s/g - 1, and subtracting that 1
puts g back in. Measured on simulated ground truth: a true depth ratio of 1.75
reads as 1.51, 1.73 or 2.51 depending on whether g is taken as 0.55, 0.62 or
0.70. Ratios are as calibration-hungry as absolute ranges.

What IS free, with no calibration of any kind, is the ORDERING. s is monotonic
in 1/rho for any positive g, so a faster-sliding object is nearer, full stop.
"Is the mug closer than the monitor" needs no ruler and no servo measurement,
and is most of what a desk robot actually needs. See `ordering()`.

Anything quantitative needs exactly one external fact, and the module refuses
to invent it:

  * `r`, the camera's horizontal offset from the base axis, in whatever unit
    the answer should come out in. One ruler measurement at a fixed shape.
  * `g`, or something standing in for it. The cheapest substitute is the
    scene itself: the SLOWEST-moving track is the most distant, and treating
    it as effectively infinite makes g = s_min. That is an assumption, not a
    measurement, and it is recorded as one in the result. It biases every
    range LONG -- the assumed gain is too high, so the computed parallax is
    too small. With nothing further away than 0.70 m in view, an object at a
    true 0.40 m reads as 1.20 m. Three times out, in the safe-looking
    direction of "further away than it is".

Only base_yaw is modelled here. The wrist swings the camera on its own arc and
gives a second, independent estimate from vertical parallax -- a cross-check
worth having, since an error in r shows up in both but a tracking error in one
axis does not.
"""
import math

# Below this the fit is noise: a pan that moved a few pixels cannot be asked
# for a slope. Chosen in commanded degrees because that is what is commanded.
MIN_SWING_DEG = 6.0

# A track whose shift is not close to linear in the commanded angle has
# probably been confused with another object, or is itself moving. Depth from
# a bad correspondence is worse than no depth.
MIN_R2 = 0.90

# A rate at or below the far-field rate means "no measurable parallax", which
# is a true statement about something far away and NOT a distance. Reported as
# a floor rather than divided by, because 1/(s/g - 1) explodes there.
MIN_PARALLAX = 0.02


class NotMeasurable(Exception):
    """The data cannot answer the question. Raised rather than returning a
    number nobody should act on."""


def _linfit(xs, ys):
    """Least-squares slope, intercept and r^2."""
    n = len(xs)
    if n < 2:
        raise NotMeasurable(f"a slope needs two points, got {n}")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        raise NotMeasurable("the arm did not move; every sample is one angle")
    slope = sxy / sxx
    intercept = my - slope * mx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 if syy == 0 else max(0.0, 1.0 - (
        sum((y - (slope * x + intercept)) ** 2
            for x, y in zip(xs, ys)) / syy))
    return slope, intercept, r2


class Track:
    """One object seen across a pan: image x positions against commanded yaw.

    Stores pixels and degrees, because that is what the hardware reports, and
    converts at the last moment -- an early conversion to "frames" hides which
    numbers were measured and which were derived.
    """

    def __init__(self, label, fov_deg, frame_width=1280):
        self.label = label
        self.fov_deg = float(fov_deg)
        self.frame_width = float(frame_width)
        self.yaws = []
        self.xs = []

    def add(self, commanded_yaw, cx):
        self.yaws.append(float(commanded_yaw))
        self.xs.append(float(cx))
        return self

    @property
    def swing(self):
        return (max(self.yaws) - min(self.yaws)) if self.yaws else 0.0

    def image_angles(self):
        """Image x -> angle from the optical axis, in degrees.

        A pinhole camera is linear in tan(angle), not in angle, and at the
        frame edge of a 52-degree lens the difference is a few per cent -- same
        order as the parallax being measured, so it is not ignorable.
        """
        half = math.tan(math.radians(self.fov_deg / 2.0))
        out = []
        for x in self.xs:
            off = (x - self.frame_width / 2.0) / (self.frame_width / 2.0)
            out.append(math.degrees(math.atan(off * half)))
        return out

    def rate(self):
        """Image-angle degrees per COMMANDED degree, as a positive magnitude.

        Sign is dropped on purpose. Which way the image slides is a fact about
        the mounting and is already pinned by the measured constants; the depth
        lives in the magnitude.
        """
        if self.swing < MIN_SWING_DEG:
            raise NotMeasurable(
                f"{self.label}: pan of {self.swing:.1f} deg is below the "
                f"{MIN_SWING_DEG} deg needed for a slope")
        slope, _, r2 = _linfit(self.yaws, self.image_angles())
        if r2 < MIN_R2:
            raise NotMeasurable(
                f"{self.label}: shift is not linear in the commanded angle "
                f"(r2 {r2:.3f} < {MIN_R2}); likely a mixed-up correspondence "
                f"or an object that moved")
        return abs(slope), r2

    def mean_image_angle(self):
        a = self.image_angles()
        return sum(a) / len(a)


def ranges(tracks, r=None, gain=None, known=None):
    """Range per track. Returns (dict, info).

    `r` is the camera's horizontal offset from the base axis, in the unit the
    answer should be in. Without it the ranges come back in UNITS OF r, which
    is still an ordering and still a ratio -- just not metres.

    `gain` is the commanded-to-actual ratio for base_yaw. Without it the
    slowest track is assumed far away and sets it; `info["gain_assumed"]` says
    so, because that assumption biases every range short when the farthest
    thing in view is in fact close.

    `known` pins the scale instead: {label: true_range}. Preferred over `gain`
    when available, since one tape measure beats one guess about a servo.
    """
    rates, bad = {}, {}
    for t in tracks:
        try:
            rates[t.label] = (t.rate()[0], t.mean_image_angle())
        except NotMeasurable as exc:
            bad[t.label] = str(exc)
    if len(rates) < 1:
        raise NotMeasurable("no track survived the fit: " + "; ".join(
            bad.values()) if bad else "no tracks given")

    info = {"rejected": bad, "rates": {k: v[0] for k, v in rates.items()},
            "gain_assumed": False, "scale": "r" if r is None else "absolute"}

    if known:
        label = next(iter(known))
        if label not in rates:
            raise NotMeasurable(
                f"{label} was given a known range but did not survive the fit")
        s_k, a_k = rates[label]
        rho_k = float(known[label])
        if r is None:
            raise NotMeasurable(
                "a known range fixes the scale only if r is also given; "
                "without r the answer is already in units of r")
        # s = g*(1 + (r/rho) cos a)  ->  g = s / (1 + (r/rho) cos a)
        g = s_k / (1.0 + (r / rho_k) * math.cos(math.radians(a_k)))
    elif gain is not None:
        g = float(gain)
    else:
        g = min(s for s, _ in rates.values())
        info["gain_assumed"] = True
        info["gain_from"] = min(rates, key=lambda k: rates[k][0])
        info["warning"] = (
            "gain taken from the slowest track, assumed to be at infinity. "
            "Every range below is biased LONG, and badly so if nothing in "
            "view is actually distant. Use ordering() for a result that "
            "needs no assumption.")

    if g <= 0:
        raise NotMeasurable(f"a gain of {g} is not physical")
    info["gain"] = g

    out = {}
    for label, (s, a) in rates.items():
        parallax = s / g - 1.0
        cos_a = math.cos(math.radians(a))
        if parallax <= MIN_PARALLAX:
            # No measurable parallax. True of anything far away, and not a
            # distance: dividing here would hand back a wild number.
            out[label] = {"range": None, "at_least": (
                (r or 1.0) * cos_a / MIN_PARALLAX), "parallax": parallax,
                "far": True}
            continue
        out[label] = {"range": (r or 1.0) * cos_a / parallax,
                      "parallax": parallax, "far": False}
    return out, info


def ordering(tracks):
    """Tracks sorted nearest-first. The one result that needs NO calibration.

    s = g*(1 + (r/rho) cos a) is monotonic in 1/rho for any positive g, so a
    faster-sliding object is nearer whatever the servo gain and whatever the
    baseline. Returns [(label, rate), ...], nearest first, plus the rejects.

    This is deliberately not a ratio and not a distance. Ordering is what is
    actually identifiable from one pan, and "the mug is nearer than the
    monitor" is most of what a desk-bound robot needs to act.
    """
    rates, bad = [], {}
    for t in tracks:
        try:
            rates.append((t.label, t.rate()[0]))
        except NotMeasurable as exc:
            bad[t.label] = str(exc)
    if not rates:
        raise NotMeasurable("no track survived the fit: " + "; ".join(
            bad.values()) if bad else "no tracks given")
    rates.sort(key=lambda kv: -kv[1])
    return rates, {"rejected": bad}


def simulate(label, true_range, r, gain, fov_deg, yaws, frame_width=1280,
             bearing_deg=0.0):
    """The exact forward model, for tests and for sanity-checking a rig.

    Deliberately NOT the linearisation the estimator inverts: a test that
    generates data from the estimator's own approximation proves only that the
    algebra was copied correctly. This places a real point in the plane and
    projects it through a real pinhole.
    """
    t = Track(label, fov_deg, frame_width)
    # The point sits at bearing `bearing_deg` from the camera's start, at
    # camera-distance `true_range`, with the camera at radius r looking
    # radially outward.
    th0 = math.radians(yaws[0] * gain)
    cam0 = (r * math.cos(th0), r * math.sin(th0))
    look0 = th0 + math.radians(bearing_deg)
    point = (cam0[0] + true_range * math.cos(look0),
             cam0[1] + true_range * math.sin(look0))
    half = math.tan(math.radians(fov_deg / 2.0))
    for y in yaws:
        th = math.radians(y * gain)
        cam = (r * math.cos(th), r * math.sin(th))
        v = (point[0] - cam[0], point[1] - cam[1])
        a = math.atan2(v[1], v[0]) - th          # angle from the optical axis
        a = (a + math.pi) % (2 * math.pi) - math.pi
        off = math.tan(a) / half
        t.add(y, frame_width / 2.0 + off * frame_width / 2.0)
    return t
