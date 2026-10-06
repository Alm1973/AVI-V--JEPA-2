"""The gesture library of BUILD_SPEC 6.3.

TWO SPEED NUMBERS, AND WHY
--------------------------
`speed_cap_deg_s` (60) is a SAFETY clamp on what may be commanded, enforced in
firmware and again host-side. `achievable_deg_s` is what the arm can ACTUALLY
do, measured at about 28 deg/s on 2026-10-05 by raising the firmware cap step
by step and timing a 60 degree sweep optically: it plateaus, and a commanded
140 is no faster than a commanded 60.

Durations and amplitudes are derived from the *achievable* figure, because a
gesture designed for a speed the arm cannot reach is not refused -- it is
silently smeared into a different shape, and nothing in the logs says so. A
shake that cannot reach its reversal in time reads as a sway.

Peak speed is not average speed: an easing curve's steepest moment is a
multiple of its average, and these are the measured multipliers:

    linear 1.00x   ease_in_out 1.57x   ease_in / ease_out 3.00x
    every *_back curve 4.70x

So "base yaw +-10, twice, in 0.9 s" needs a peak of 218 deg/s, and perk_up's
22 deg rise on an overshoot curve needs 146 deg/s. Asking for them anyway is
not unsafe -- the limiter and the firmware both clamp -- but it is worse than
unsafe, it is *silently wrong*: the gesture gets smeared into a different
shape, so a shake reads as a slow sway and nobody can tell from the logs.

Each gesture below keeps the spec's shape and timing, and _kf/_pose shrink its
amplitude (or stretch a pose move's duration) to whatever the arm can do. So
the numbers written here are the INTENT, and the arm performs the largest
version of that intent it is capable of. Raise achievable_deg_s once the 5 V
4 A supply is in and every gesture gets its size back with no code change.

Fast oscillations use linear interpolation, since an ease between every
keyframe of a shake both costs pi/2 in peak speed and makes the motion stutter.

WHICH GESTURES ARE VISIBLE TODAY
--------------------------------
Only channels 0 (base yaw) and 1 (shoulder) are driven. The elbow and wrist
signs are unknown, so safety.Limiter pins them to center and a gesture made
only of wrist motion does nothing at all on hardware until M1 measures them:

    visible now : shake, perk_up, droop, excited_wiggle, celebrate, startled,
                  thinking, ahem, doze, yawn_wake, embarrassed (yaw only),
                  curious_lean (shoulder only)
    no-op now   : nod

Build the sim with drive_undriven=True to preview the full motion.
"""
import math

from . import poses
from .engine import HoldMove, KeyframeMove, PoseMove, SequenceMove

_CFG = None


def _rate():
    """What the arm can actually do, from config/joints.toml.

    MEASURED, not the safety cap: about 28 deg/s on 2026-10-05, roughly ten
    times slower than these servos' datasheet, which points at the UBEC
    current-limiting them rather than at the servos. When the 5 V 4 A supply is
    installed, re-measure and raise achievable_deg_s -- every gesture below
    gets its snap back from that one edit, with no code change.
    """
    global _CFG
    if _CFG is None:
        _CFG = poses.load()
    return _CFG.achievable


# Gestures give up TEMPO rather than SIZE when the arm cannot keep up.
#
# This was the other way round, and it was wrong on this hardware. The arm
# manages 27 deg/s against the 60 the gesture table was written for, so
# shrinking put everything at about 45% amplitude -- fidget commanding 2.7
# degrees, shake 4.5 -- and Shaurya's verdict on watching it idle was "kinda
# just standing there... not doing much". An invisible shake communicates
# nothing, so a slow visible one is strictly better.
#
# The shrink strategy is kept because the mechanism is right even though
# nothing uses it today: a gesture whose rhythm genuinely cannot be slowed
# would want it. And the stretch reverses itself -- the factor comes from
# achievable_deg_s, so raising that after the 5 V 4 A supply goes in returns
# every gesture to its specified tempo with no code change.
DEFAULT_FIT = "stretch"


def _kf(name, keys, duration, ease="ease_in_out", fit=DEFAULT_FIT):
    """A keyframe curve fitted to what the arm can actually do."""
    return KeyframeMove(name, keys, duration, ease, max_rate=_rate(), fit=fit)


def _pose(name, target, duration, ease="ease_in_out"):
    """A pose move that stretches its duration to fit what the arm can do."""
    return PoseMove(name, target, duration, ease, max_rate=_rate())

# Travel in degrees that each easing may cover per second at the 60 deg/s cap.
# Kept here as documentation; the test is what actually enforces it.
# ease_in_out's peak is exactly pi/2; the *_back curves' is 1 + 2*c1 + 3*c3
# with c1 = 1.70158. Written exactly rather than rounded, because a rounded
# 1.57 puts a 40-degree ahem 0.03 deg/s over the cap.
PEAK_MULTIPLIER = {"linear": 1.0, "ease_in_out": math.pi / 2, "ease_out": 3.0,
                   "ease_in": 3.0, "ease_out_back": 4.70,
                   "ease_in_back": 4.70, "ease_in_out_back": 4.70}
SPEED_CAP_MARGIN = 1.02          # ask for 2% less than the cap, not exactly it


def nod():
    """Wrist pitch -8 -> +4, twice.  spec 0.8 s -> 1.1 s at the cap.

    WRIST-ONLY, so it is a no-op on hardware today.
    """
    return _kf("nod", [
        (0.0000, {"wrist": 0.0}),
        (0.1250, {"wrist": -8.0}),
        (0.3125, {"wrist": 4.0}),
        (0.5000, {"wrist": 0.0}),
        (0.6250, {"wrist": -8.0}),
        (0.8125, {"wrist": 4.0}),
        (1.0000, {"wrist": 0.0}),
    ], duration=1.1, ease="linear")


def shake():
    """Base yaw +-10, twice.  spec 0.9 s -> 1.35 s at the cap.

    A +-10 reversal is 20 deg of travel; twice inside 0.9 s simply is not
    something a 60 deg/s joint can do.
    """
    return _kf("shake", [
        (0.000, {"base_yaw": 0.0}),
        (0.125, {"base_yaw": -10.0}),
        (0.375, {"base_yaw": 10.0}),
        (0.625, {"base_yaw": -10.0}),
        (0.875, {"base_yaw": 10.0}),
        (1.000, {"base_yaw": 0.0}),
    ], duration=1.35, ease="linear")


def perk_up():
    """Rise to TALL with a small overshoot.

    THE EASING CURVE IS THE WHOLE COST. Measured at 10.18 s on hardware
    2026-10-05: HOME and TALL are 88.5 degrees apart at the wrist, ease_out
    costs 3.00x in peak speed across all of it, and the arm manages 27 deg/s.
    ease_in_out costs 1.571x and nearly halves it.

    The overshoot is a separate fixed-size leg rather than a back curve over
    the whole travel, so it stays sharp however far the arm had to come -- and
    a back curve here would cost 4.70x, which is how yawn_wake reached 16 s.
    """
    return SequenceMove("perk_up", [
        _pose("perk_rise", "TALL", 1.1, ease="ease_in_out"),
        _kf("perk_overshoot", [
            (0.0, {}), (0.4, {"shoulder": -3.0}), (1.0, {}),
        ], duration=0.35, ease="linear"),
    ])


def droop():
    """Slow ease down to LOW, wrist down last.  wrist leg 0.3 s -> 0.5 s."""
    return SequenceMove("droop", [
        _pose("droop_body", "LOW", 1.2, ease="ease_in_out"),
        _kf("droop_wrist", [(0.0, {}), (1.0, {"wrist": 12.0})],
                     duration=0.5, ease="ease_in_out"),
    ])


def curious_lean(hold=1.0):
    """Shoulder and elbow extend toward the target, wrist eases, then hold.

    There is no roll joint, so "curious" is a lean plus a wrist pitch rather
    than a head tilt (BUILD_SPEC section 3). The hold really holds the lean:
    SequenceMove carries each finished leg's offset forward.
    """
    return SequenceMove("curious_lean", [
        _kf("lean", [
            (0.0, {}),
            (1.0, {"shoulder": -7.0, "elbow": -9.0, "wrist": -5.0}),
        ], duration=1.2, ease="ease_out"),
        HoldMove(hold, name="curious_hold"),
    ])


def excited_wiggle():
    """Fast +-6 yaw and wrist bob, 3 cycles.  spec 0.9 s -> 1.2 s at the cap."""
    keys = [(0.0, {"base_yaw": 0.0, "wrist": 0.0})]
    for i in range(3):
        sign = 1.0 if i % 2 == 0 else -1.0
        keys.append(((i + 0.5) / 3.0, {"base_yaw": 6.0 * sign, "wrist": -4.0}))
        keys.append(((i + 1.0) / 3.0, {"base_yaw": -6.0 * sign, "wrist": 2.0}))
    keys.append((1.0, {"base_yaw": 0.0, "wrist": 0.0}))
    return _kf("excited_wiggle", keys, duration=1.2, ease="linear")


def celebrate():
    """perk_up + excited_wiggle.  spec 1.8 s -> 2.65 s, the sum of its parts.

    The happy chirp belongs to the voice layer, not here.
    """
    return SequenceMove("celebrate", [perk_up(), excited_wiggle()])


def startled():
    """Quick pull-back 6, freeze 0.3 s, slow return.  1.2 s, linear.

    The recoil is linear on purpose: an ease_out recoil costs 3.00x and would
    need 1 s of the 1.2 s budget just for the first 6 degrees.
    """
    return _kf("startled", [
        (0.00, {}),
        (0.10, {"shoulder": 6.0, "elbow": 6.0}),   # recoil, away from the desk
        (0.35, {"shoulder": 6.0, "elbow": 6.0}),   # freeze
        (1.00, {}),
    ], duration=1.2, ease="linear")


def thinking():
    """Slow look up and to one side, pause, return.  1.5 s, as specified."""
    return _kf("thinking", [
        (0.00, {}),
        # +wrist looks up, which is what "look up and to one side" wants.
        (0.33, {"base_yaw": -9.0, "shoulder": -5.0, "wrist": 6.0}),
        (0.66, {"base_yaw": -9.0, "shoulder": -5.0, "wrist": 6.0}),
        (1.00, {}),
    ], duration=1.5, ease="ease_in_out")


def embarrassed():
    """Wrist down, small yaw away, slow return.  1.4 s, as specified.

    Only the 5 deg yaw shows on hardware today.
    """
    return _kf("embarrassed", [
        (0.00, {}),
        # -wrist looks down, which is the point of looking embarrassed.
        (0.25, {"wrist": -10.0, "base_yaw": -5.0}),
        (0.55, {"wrist": -10.0, "base_yaw": -5.0}),
        (1.00, {}),
    ], duration=1.4, ease="ease_in_out")


def ahem(yaw=0.0):
    """Slow turn toward the target, then a small nod.

    The turn's duration scales with how far it has to go, so `ahem` toward
    something 40 deg off center takes longer instead of being clamped into a
    different gesture. `yaw` is degrees off center, + is to the right.
    """
    turn = max(0.7, abs(float(yaw)) * PEAK_MULTIPLIER["ease_in_out"]
               * SPEED_CAP_MARGIN / 60.0)
    return SequenceMove("ahem", [
        _kf("ahem_turn", [(0.0, {}), (1.0, {"base_yaw": float(yaw)})],
                     duration=turn, ease="ease_in_out"),
        _kf("ahem_nod", [
            (0.0, {}), (0.5, {"wrist": -6.0}), (1.0, {}),
        ], duration=0.4, ease="linear"),
    ])


def doze():
    """Ease to REST and stay. The caller deepens breathing; see DOZE_BREATH."""
    m = _pose("doze", "REST", 2.0, ease="ease_in_out")
    m.continuous = True          # stays primary until something interrupts it
    return m


DOZE_BREATH = 2.2               # multiply Breathing.depth by this while dozing


def yawn_wake():
    """Slow rise from REST with a stretch overshoot.

    Measured at 16.01 s on hardware before this was split: REST and HOME are
    about 92 degrees apart at the elbow, and ease_out_back costs 4.70x in peak
    speed over the lot, which at 27 deg/s is simply what that arithmetic gives.
    A sixteen-second yawn is not a yawn.

    So the travel uses ease_in_out at 1.571x and the stretch is a separate
    fixed-size leg. A yawn should look slow, but slow on purpose rather than
    slow because of an easing curve nobody costed.
    """
    return SequenceMove("yawn_wake", [
        _pose("yawn_rise", "HOME", 2.4, ease="ease_in_out"),
        _kf("yawn_stretch", [
            (0.0, {}), (0.45, {"shoulder": -4.0, "wrist": 4.0}), (1.0, {}),
        ], duration=0.6, ease="linear"),
    ])


GESTURES = {
    "nod": nod, "shake": shake, "perk_up": perk_up, "droop": droop,
    "curious_lean": curious_lean, "excited_wiggle": excited_wiggle,
    "celebrate": celebrate, "startled": startled, "thinking": thinking,
    "embarrassed": embarrassed, "ahem": ahem, "doze": doze,
    "yawn_wake": yawn_wake,
}

# Nothing is a complete no-op any more: the wrist is driven. Kept as an empty
# tuple rather than deleted, because the elbow is still pinned and an
# elbow-only gesture added later belongs here.
NO_OP_UNTIL_M1 = ()

# A gesture only makes sense from some poses. yawn_wake from HOME is a no-op by
# definition, so tests and the dashboard start it where it belongs.
START_POSE = {"yawn_wake": "REST"}


def make(name, **kw):
    try:
        return GESTURES[name](**kw)
    except KeyError:
        raise KeyError(f"unknown gesture {name!r}; have {sorted(GESTURES)}")
