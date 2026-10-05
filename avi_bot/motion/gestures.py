"""The gesture library of BUILD_SPEC 6.3.

SPEED CAP, AND WHY SOME DURATIONS DIFFER FROM THE SPEC
------------------------------------------------------
Safety caps every joint at 60 deg/s in firmware (BUILD_SPEC 6.4), and the
*current* sketch is slower still at about 37 deg/s. Several of the spec's
gesture values ask for more than that, because peak speed is not average
speed: an easing curve's steepest moment is a multiple of its average, and
these are the measured multipliers:

    linear 1.00x   ease_in_out 1.57x   ease_in / ease_out 3.00x
    every *_back curve 4.70x

So "base yaw +-10, twice, in 0.9 s" needs a peak of 218 deg/s, and perk_up's
22 deg rise on an overshoot curve needs 146 deg/s. Asking for them anyway is
not unsafe -- the limiter and the firmware both clamp -- but it is worse than
unsafe, it is *silently wrong*: the gesture gets smeared into a different
shape, so a shake reads as a slow sway and nobody can tell from the logs.

Each gesture below is therefore the fastest version of the spec's shape that
fits 60 deg/s, with the spec's own value kept in a comment. Fast oscillations
use linear interpolation, since an ease between every keyframe of a shake both
costs 1.57x and makes the motion stutter. If the arm turns out to manage more
than 60 deg/s, raise speed_cap_deg_s in config/joints.toml and shorten these
again; tests/test_motion.py recomputes the whole table against whatever the cap
says.

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

from .engine import HoldMove, KeyframeMove, PoseMove, SequenceMove

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
    return KeyframeMove("nod", [
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
    return KeyframeMove("shake", [
        (0.000, {"base_yaw": 0.0}),
        (0.125, {"base_yaw": -10.0}),
        (0.375, {"base_yaw": 10.0}),
        (0.625, {"base_yaw": -10.0}),
        (0.875, {"base_yaw": 10.0}),
        (1.000, {"base_yaw": 0.0}),
    ], duration=1.35, ease="linear")


def perk_up():
    """Rise to TALL with a small overshoot.  spec 0.7 s -> 1.45 s at the cap.

    The spec asks for anticipation *and* overshoot out of one back curve, but
    that curve's 4.70x peak would need 1.7 s for this travel anyway. Splitting
    it into an eager ease_out rise plus a separate 3 deg overshoot leg keeps the
    overshoot, costs less peak speed, and reads better: at 60 deg/s an
    anticipation dip is too slow to register as a wind-up.
    """
    return SequenceMove("perk_up", [
        PoseMove("perk_rise", "TALL", 1.1, ease="ease_out"),
        KeyframeMove("perk_overshoot", [
            (0.0, {}), (0.4, {"shoulder": -3.0}), (1.0, {}),
        ], duration=0.35, ease="linear"),
    ])


def droop():
    """Slow ease down to LOW, wrist down last.  wrist leg 0.3 s -> 0.5 s."""
    return SequenceMove("droop", [
        PoseMove("droop_body", "LOW", 1.2, ease="ease_in_out"),
        KeyframeMove("droop_wrist", [(0.0, {}), (1.0, {"wrist": 12.0})],
                     duration=0.5, ease="ease_in_out"),
    ])


def curious_lean(hold=1.0):
    """Shoulder and elbow extend toward the target, wrist eases, then hold.

    There is no roll joint, so "curious" is a lean plus a wrist pitch rather
    than a head tilt (BUILD_SPEC section 3). The hold really holds the lean:
    SequenceMove carries each finished leg's offset forward.
    """
    return SequenceMove("curious_lean", [
        KeyframeMove("lean", [
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
    return KeyframeMove("excited_wiggle", keys, duration=1.2, ease="linear")


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
    return KeyframeMove("startled", [
        (0.00, {}),
        (0.10, {"shoulder": 6.0, "elbow": 6.0}),   # recoil, away from the desk
        (0.35, {"shoulder": 6.0, "elbow": 6.0}),   # freeze
        (1.00, {}),
    ], duration=1.2, ease="linear")


def thinking():
    """Slow look up and to one side, pause, return.  1.5 s, as specified."""
    return KeyframeMove("thinking", [
        (0.00, {}),
        (0.33, {"base_yaw": -9.0, "shoulder": -5.0, "wrist": -6.0}),
        (0.66, {"base_yaw": -9.0, "shoulder": -5.0, "wrist": -6.0}),
        (1.00, {}),
    ], duration=1.5, ease="ease_in_out")


def embarrassed():
    """Wrist down, small yaw away, slow return.  1.4 s, as specified.

    Only the 5 deg yaw shows on hardware today.
    """
    return KeyframeMove("embarrassed", [
        (0.00, {}),
        (0.25, {"wrist": 10.0, "base_yaw": -5.0}),
        (0.55, {"wrist": 10.0, "base_yaw": -5.0}),
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
        KeyframeMove("ahem_turn", [(0.0, {}), (1.0, {"base_yaw": float(yaw)})],
                     duration=turn, ease="ease_in_out"),
        KeyframeMove("ahem_nod", [
            (0.0, {}), (0.5, {"wrist": -6.0}), (1.0, {}),
        ], duration=0.4, ease="linear"),
    ])


def doze():
    """Ease to REST and stay. The caller deepens breathing; see DOZE_BREATH."""
    m = PoseMove("doze", "REST", 2.0, ease="ease_in_out")
    m.continuous = True          # stays primary until something interrupts it
    return m


DOZE_BREATH = 2.2               # multiply Breathing.depth by this while dozing


def yawn_wake():
    """Slow rise from REST with a stretch overshoot.  spec 2.0 s -> 2.8 s.

    35 deg of travel on a 4.70x overshoot curve needs 2.74 s at the cap.
    """
    return PoseMove("yawn_wake", "HOME", 2.8, ease="ease_out_back")


GESTURES = {
    "nod": nod, "shake": shake, "perk_up": perk_up, "droop": droop,
    "curious_lean": curious_lean, "excited_wiggle": excited_wiggle,
    "celebrate": celebrate, "startled": startled, "thinking": thinking,
    "embarrassed": embarrassed, "ahem": ahem, "doze": doze,
    "yawn_wake": yawn_wake,
}

# Gestures that are wrist-only, so they do nothing on hardware until
# config/joints.toml gets a real sign for channel 3.
NO_OP_UNTIL_M1 = ("nod",)

# A gesture only makes sense from some poses. yawn_wake from HOME is a no-op by
# definition, so tests and the dashboard start it where it belongs.
START_POSE = {"yawn_wake": "REST"}


def make(name, **kw):
    try:
        return GESTURES[name](**kw)
    except KeyError:
        raise KeyError(f"unknown gesture {name!r}; have {sorted(GESTURES)}")
