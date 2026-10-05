"""Easing curves, written out rather than pulled in from pytweening.

Every function maps [0, 1] -> roughly [0, 1]. The "back" curves deliberately
leave that range (that is the whole point of them: anticipation dips below 0,
overshoot rises above 1), so the motion engine clamps afterwards and nothing
here needs to.
"""
import math

_C1 = 1.70158          # the classic "back" constant
_C3 = _C1 + 1.0


def linear(t):
    return t


def ease_in_out(t):
    """Sine ease. The default for anything that should read as calm."""
    return -(math.cos(math.pi * t) - 1.0) / 2.0


def ease_out(t):
    return 1.0 - (1.0 - t) ** 3


def ease_in(t):
    return t * t * t


def ease_out_back(t):
    """Overshoots past 1 and settles. This is "bounce" in the mood modifiers."""
    return 1.0 + _C3 * (t - 1.0) ** 3 + _C1 * (t - 1.0) ** 2


def ease_in_back(t):
    """Dips below 0 first. This is anticipation: wind up, then go."""
    return _C3 * t * t * t - _C1 * t * t


def ease_in_out_back(t):
    if t < 0.5:
        return ((2 * t) ** 2 * ((_C1 + 1) * 2 * t - _C1)) / 2.0
    return ((2 * t - 2) ** 2 * ((_C1 + 1) * (2 * t - 2) + _C1) + 2) / 2.0


BY_NAME = {
    "linear": linear,
    "ease_in": ease_in,
    "ease_out": ease_out,
    "ease_in_out": ease_in_out,
    "ease_out_back": ease_out_back,
    "ease_in_back": ease_in_back,
    "ease_in_out_back": ease_in_out_back,
}


# How much steeper each curve's steepest moment is than its average, measured
# by sampling at 20 000 points. A move's peak rate is
#     travel / duration * PEAK[ease]
# and that peak is what has to fit inside what the arm can actually do.
PEAK = {
    "linear": 1.0,
    "ease_in": 3.0,
    "ease_out": 3.0,
    "ease_in_out": math.pi / 2,
    "ease_out_back": 4.70,
    "ease_in_back": 4.70,
    "ease_in_out_back": 4.70,
}


def peak_of(name):
    return PEAK[name]


def get(name):
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown easing {name!r}; have {sorted(BY_NAME)}")
