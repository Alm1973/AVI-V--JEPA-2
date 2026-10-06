"""Pixels to view angle. One place, because there were two.

`dome.py` converted a pixel offset to an angle linearly, `depth.py` through a
pinhole arctangent, and both are described as "the angle from the optical
axis". That is the same failure find.py's docstring warns about -- two
implementations of one geometry, which is how the sentence and the arm come to
disagree -- and it was committed here a few hours after writing that warning.

The two are NOT interchangeable, and which one is right depends on what is
being computed:

  LINEAR is the convention every measured constant in this project was fitted
  with. `yaw_deg_per_frame = 83.9` and `wrist_deg_per_frame = 32.2` came from
  a straight line through pixel offset against commanded degrees. Those
  numbers MEAN "linear", so the tracking loop, roommap and find must keep
  using it. Swapping in a better model without refitting the constants would
  make a correct formula produce worse answers.

  PINHOLE is the physics. A real lens is linear in tan(angle), not in angle,
  so the linear version overstates the angle near the frame edge. depth.py
  needs it because parallax is a few per cent of the shift and the
  approximation error is the same size -- it would be read as depth.

The difference is zero at the centre, zero at the edge by construction, and
largest in between. For a 52 degree lens it peaks near 4%, which is small
enough to have gone unnoticed and large enough to invent a depth reading.
"""
import math


def angle_linear(offset_px, span_px, fov_deg):
    """Angle from the optical axis, assuming pixels map linearly to degrees.

    The convention the project's measured deg-per-frame constants embed. Use
    it anywhere those constants are used.
    """
    return (float(offset_px) / float(span_px)) * float(fov_deg)


def angle_pinhole(offset_px, span_px, fov_deg):
    """Angle from the optical axis through a real pinhole projection.

    `offset_px` is measured from the centre; `span_px` is the FULL width or
    height, so an offset of span/2 is the frame edge and returns fov/2.
    """
    half = math.tan(math.radians(float(fov_deg) / 2.0))
    return math.degrees(math.atan(
        (float(offset_px) / (float(span_px) / 2.0)) * half))


def disagreement(span_px, fov_deg, steps=101):
    """Worst gap between the two, in degrees. For deciding if it matters."""
    worst = 0.0
    for i in range(steps):
        off = (-0.5 + i / (steps - 1)) * span_px
        worst = max(worst, abs(angle_linear(off, span_px, fov_deg)
                               - angle_pinhole(off, span_px, fov_deg)))
    return worst
