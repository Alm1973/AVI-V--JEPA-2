"""Pixels to view angle, pinned so the two conventions cannot drift.

dome.py converted linearly, depth.py through an arctangent, and both called
the result "the angle from the optical axis". Two implementations of one
geometry is the failure find.py's own docstring warns about, committed a few
hours after that warning was written.
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import optics                          # noqa: E402
from avi_bot.perception import depth as depth_mod              # noqa: E402

W, H, FOV = 1280, 720, 52.0


class TheyAgreeWhereTheyMust(unittest.TestCase):
    def test_both_are_zero_at_the_centre(self):
        self.assertEqual(optics.angle_linear(0, W, FOV), 0.0)
        self.assertEqual(optics.angle_pinhole(0, W, FOV), 0.0)

    def test_both_give_half_the_fov_at_the_edge(self):
        for fn in (optics.angle_linear, optics.angle_pinhole):
            with self.subTest(fn.__name__):
                self.assertAlmostEqual(fn(W / 2, W, FOV), FOV / 2, places=9)
                self.assertAlmostEqual(fn(-W / 2, W, FOV), -FOV / 2, places=9)

    def test_they_differ_in_between_and_by_how_much(self):
        """Zero at both ends and largest in the middle, which is why it goes
        unnoticed. 4% of a 52 degree lens is the same size as the parallax
        depth.py measures, so it would be read as depth."""
        gap = optics.disagreement(W, FOV)
        self.assertAlmostEqual(gap, 0.7166, places=3)

    def test_linear_UNDERstates_the_angle_in_between(self):
        """Direction checked rather than assumed; the first version had it
        backwards. tan(angle) is proportional to pixel offset, so
        angle = atan(k * px) is concave, and being pinned at both the centre
        and the edge it runs ABOVE the straight line everywhere between.

        Measured across the frame, not asserted: the gap peaks at 0.72 deg
        (linear 13.000, pinhole 13.705 at a quarter width) and closes to zero
        at both the centre and the edge.
        """
        for off in (W * 0.1, W * 0.2, W * 0.35):
            with self.subTest(off=off):
                self.assertLess(optics.angle_linear(off, W, FOV),
                                optics.angle_pinhole(off, W, FOV))
        self.assertAlmostEqual(optics.angle_linear(W * 0.25, W, FOV), 13.0,
                               places=6)
        self.assertAlmostEqual(optics.angle_pinhole(W * 0.25, W, FOV), 13.705,
                               places=3)


class DepthUsesThePinhole(unittest.TestCase):
    def test_track_angles_match_the_shared_function(self):
        t = depth_mod.Track("x", FOV, W)
        for cx in (0, 200, 640, 1100, 1280):
            t.add(60, cx)
        got = t.image_angles()
        want = [optics.angle_pinhole(cx - W / 2.0, W, FOV)
                for cx in (0, 200, 640, 1100, 1280)]
        for a, b in zip(got, want):
            self.assertAlmostEqual(a, b, places=12)

    def test_it_is_not_the_linear_one(self):
        """If this ever passes, depth has silently switched convention and its
        ranges are wrong by about the size of the parallax."""
        t = depth_mod.Track("x", FOV, W)
        t.add(60, 1100)
        self.assertNotAlmostEqual(
            t.image_angles()[0],
            optics.angle_linear(1100 - W / 2.0, W, FOV), places=2)


class TheVerticalAxisToo(unittest.TestCase):
    def test_the_height_span_is_used_for_cy(self):
        vfov = 31.0
        self.assertAlmostEqual(optics.angle_pinhole(H / 2, H, vfov),
                               vfov / 2, places=9)
        self.assertAlmostEqual(optics.angle_linear(H / 2, H, vfov),
                               vfov / 2, places=9)
