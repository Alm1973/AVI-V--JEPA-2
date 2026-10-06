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
# POST /probe_backlash, 2026-10-06, at wrist 150 (near level): arriving at
# yaw 90 from above vs from below differs by 13.5 and 12.7 deg (repeat
# 0.8). Looking DOWN the arm carries more load and the effective slack grows:
# /probe_horizon's 12 deg run-ups lost 1.5-4 deg of the next 8 deg turn, i.e.
# ~14-16 deg. 18 covers both with a little margin; every degree more costs a
# degree of survey coverage at the soft limit, where a run-up cannot fit.
YAW_BACKLASH_DEG = 18.0


def plan(current, target, lo, backlash=YAW_BACKLASH_DEG, settled=False):
    """The commands that put the joint at `target`, ending in an upward run
    long enough to take up the slack.

    `current` is the last COMMANDED angle (None when unknown). One command is
    enough when the move is itself upward by at least the backlash, or when
    the slack is already known to be taken up (`settled`) and the move is
    upward. Anything else gets a run-up from `backlash` below the target --
    including a SHORT upward move after something else moved the joint down:
    found when /find turned 10.9 deg up from where a survey had parked the arm
    going down, and the laptop it was aiming at never came into view.
    When the soft limit leaves no room, the run-up is as long as it allows.
    """
    target = float(target)
    if current is not None:
        up = target - float(current)
        if up >= float(backlash) - 1e-6 or (settled and up >= -1e-6):
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


class Slack:
    """Is base_yaw's backlash taken up right now?

    Only upward travel of at least the backlash, with no downward command
    since, guarantees it. A run-up the soft limit cut short does not: the
    survey's first column sits at yaw 20.7 with soft_min 20.0, got a 0.7 deg
    run-up, and filed a pen 11 degrees from where every other stop put it --
    the next 13 degree step moved the pen 32 px instead of ~280, the slack
    being taken up. Stops that are not settled are not trusted for position.
    """

    def __init__(self, backlash=YAW_BACKLASH_DEG):
        self.backlash = float(backlash)
        self.last = None
        self.up_run = 0.0

    def command(self, current, target):
        """Record a command; returns whether the slack is now taken up.
        A `current` that is not the last commanded target means something
        else moved the joint, so the history is void."""
        target = float(target)
        if (current is None or self.last is None
                or abs(float(current) - self.last) > 1e-6):
            self.up_run = 0.0
        step = target - (float(current) if current is not None else target)
        self.up_run = self.up_run + step if step >= 0 else 0.0
        self.last = target
        return self.settled

    @property
    def settled(self):
        return self.up_run >= self.backlash - 1e-6
