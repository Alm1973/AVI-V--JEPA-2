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

# THE OTHER JOINTS HAVE SLACK TOO. POST /babble, 2026-10-09 ~08:05, at
# ATTENTIVE: 3-6 degree moves out and straight back, measured by ORB. The
# base, sent through plan(), came back within 4 px every time; the others
# stopped short on the return, the same way whichever side they went:
#
#     wrist     +33 +41 -19 -37 px    (~1.5 deg; ~25 px per degree)
#     elbow     -11 +9 -25 +22 px     (~1 deg)
#     shoulder  -0 -28 +12 -14 px     (~1 deg)
#
# and two dances ended 23 and 39 px off vertically. So every joint arrives
# from one side, after a run-up of at least its slack: base, elbow and wrist
# going UP in logical degrees; the shoulder going DOWN, because ATTENTIVE --
# where AVI spends its day -- sits 4.5 degrees above the shoulder's soft
# minimum, with no room for a run-up from below.
ARRIVE = {"base_yaw": +1, "shoulder": -1, "elbow": +1, "wrist": +1}
SLACK_DEG = {"base_yaw": YAW_BACKLASH_DEG, "shoulder": 4.0, "elbow": 4.0,
             "wrist": 4.0}


def run_up(joint, target, limits):
    """Where to send `joint` first so that the move to `target` arrives
    from its ARRIVE side, taking up SLACK_DEG; None when there is no room
    (the soft limit is right there) or nothing to do."""
    lo, hi = limits
    side = ARRIVE[joint]
    pre = float(target) - side * SLACK_DEG[joint]
    pre = max(float(lo), min(float(hi), pre))
    return pre if abs(pre - float(target)) >= 1.0 else None


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


def column_order(stops, ndigits=1):
    """Survey stops reordered column by column: base_yaw ascending, and
    within each column the wrist DESCENDING.

    Row by row, every elevation row swept the base across its whole range
    and jumped back with an 18 deg run-up -- ~290 deg of base travel per row,
    ~2600 deg for a dense survey, on the joint that stalls under sustained
    work. Column by column the base only ever steps UP, once per column, so
    it never reverses and never needs a run-up after the first: ~130 deg in
    total. The up-and-down work moves to the wrist, which carries only the
    camera.
    """
    return sorted(stops, key=lambda s: (round(float(s[0]), ndigits),
                                        -float(s[1])))
