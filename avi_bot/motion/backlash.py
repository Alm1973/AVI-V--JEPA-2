"""Arrive at every base_yaw target from the same side.

MEASURED 2026-10-06 by POST /probe_horizon: base_yaw's backlash is between 12
and 24 degrees. With a 12 degree run-up, the first 8 degree turn after a
reversal travelled only ~0.55 of the second; with 24, the two agreed to within
noise at every tilt. So any target reached by moving DOWN is off by up to the
backlash, and every survey row (which starts by jumping back to its lowest
azimuth) had its first column in the wrong place.

The fix is the usual one: never finish a move going down. Overshoot below
the target by the backlash, then come up.
"""
YAW_BACKLASH_DEG = 24.0


def plan(current, target, lo, backlash=YAW_BACKLASH_DEG):
    """The commands that put the joint at `target`, last move upward.

    `current` is the last COMMANDED angle (None when unknown, which is
    treated as a reversal). Moving up already takes up the slack, so it is
    one command. Moving down needs a run-up from below; when the soft limit
    leaves no room, the run-up is as long as the limit allows.
    """
    target = float(target)
    if current is not None and target >= float(current) - 1e-6:
        return [target]
    pre = max(float(lo), target - float(backlash))
    return [pre, target] if pre < target else [target]

# Measured top speed of the arm on the current supply; see HANDOFF section 4.
SPEED_DEG_S = 27.0


def travel_s(a, b, speed=SPEED_DEG_S, margin=0.3):
    """Seconds to let a move from a to b finish before reversing it. A run-up
    cut short does not take up the slack, which is the whole point of it."""
    if a is None:
        return 180.0 / speed + margin
    return abs(float(b) - float(a)) / speed + margin
