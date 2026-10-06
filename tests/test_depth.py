"""Range from the arm's own motion.

Every case here generates its data from depth.simulate, which places a real
point in the plane and projects it through a real pinhole. That is NOT the
linearisation the estimator inverts, on purpose: a test fed by the estimator's
own approximation proves only that the algebra was copied consistently, and
would hide exactly the error that matters -- how far the small-angle model
drifts from the geometry at the frame edge.
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import depth                           # noqa: E402

FOV = 52.0
R = 0.18          # metres from the base axis to the lens, a plausible arm
GAIN = 0.62       # commanded -> actual, the measured under-travel
YAWS = [50, 55, 60, 65, 70, 75]


def track(label, true_range, **kw):
    kw.setdefault("bearing_deg", 0.0)
    return depth.simulate(label, true_range, R, GAIN, FOV, YAWS, **kw)


class TheSignalExists(unittest.TestCase):
    def test_a_near_object_slides_faster_than_a_far_one(self):
        """The whole premise. If these came out equal there is no depth to
        recover and the camera might as well be on the rotation axis."""
        near = track("mug", 0.40).rate()[0]
        far = track("wall", 6.00).rate()[0]
        self.assertGreater(near, far)

    def test_the_far_field_rate_approaches_the_joint_gain(self):
        """At infinity only rotation remains, so the rate IS the gain. This is
        what makes the slowest track usable as a stand-in for it."""
        self.assertAlmostEqual(track("sky", 500.0).rate()[0], GAIN, places=2)

    def test_an_object_at_the_baseline_radius_moves_about_twice_as_fast(self):
        rate = track("close", R).rate()[0]
        self.assertAlmostEqual(rate / GAIN, 2.0, delta=0.15)

    def test_the_shift_is_linear_enough_to_fit(self):
        _, r2 = track("mug", 0.45).rate()
        self.assertGreater(r2, 0.99)


class Metric(unittest.TestCase):
    def test_it_recovers_known_ranges(self):
        truth = {"mug": 0.45, "laptop": 0.80, "monitor": 1.20, "wall": 4.00}
        got, info = depth.ranges([track(k, v) for k, v in truth.items()],
                                 r=R, gain=GAIN)
        for label, want in truth.items():
            with self.subTest(label):
                self.assertAlmostEqual(got[label]["range"], want,
                                       delta=0.05 * want)
        self.assertFalse(info["gain_assumed"])

    def test_a_known_range_can_set_the_scale_instead_of_the_gain(self):
        """One tape measure beats one guess about a servo."""
        truth = {"mug": 0.45, "laptop": 0.80, "wall": 4.00}
        got, info = depth.ranges([track(k, v) for k, v in truth.items()],
                                 r=R, known={"laptop": 0.80})
        self.assertAlmostEqual(info["gain"], GAIN, delta=0.02)
        self.assertAlmostEqual(got["mug"]["range"], 0.45, delta=0.03)

    def test_without_r_the_answer_is_in_units_of_r(self):
        truth = {"mug": 0.45, "wall": 4.00}
        got, _ = depth.ranges([track(k, v) for k, v in truth.items()],
                              gain=GAIN)
        # Same ratio, different unit.
        self.assertAlmostEqual(got["mug"]["range"] * R, 0.45, delta=0.03)

    def test_a_distant_object_is_reported_as_far_not_as_a_number(self):
        """1/(s/g - 1) explodes at the far field. A floor is honest; a wild
        number that happens to be finite is not."""
        got, _ = depth.ranges([track("sky", 2000.0)], r=R, gain=GAIN)
        self.assertTrue(got["sky"]["far"])
        self.assertIsNone(got["sky"]["range"])
        self.assertGreater(got["sky"]["at_least"], 1.0)


class WhatIsActuallyIdentifiable(unittest.TestCase):
    """s_i = g*(1+c_i) is n equations in n+1 unknowns, for ANY n. The gain is
    never recoverable from one pan, so only the ORDERING is free."""

    def test_the_ordering_needs_no_calibration_at_all(self):
        truth = {"mug": 0.40, "laptop": 0.85, "monitor": 1.40, "wall": 5.0}
        order, info = depth.ordering([track(k, v) for k, v in truth.items()])
        self.assertEqual([k for k, _ in order],
                         ["mug", "laptop", "monitor", "wall"])
        self.assertEqual(info["rejected"], {})

    def test_the_ordering_survives_any_gain(self):
        """No r, no g, no known range: the rank is still right."""
        for gain in (0.40, 0.62, 0.95, 1.30):
            with self.subTest(gain=gain):
                ts = [depth.simulate(k, v, R, gain, FOV, YAWS)
                      for k, v in (("mug", 0.40), ("wall", 5.0))]
                order, _ = depth.ordering(ts)
                self.assertEqual([k for k, _ in order], ["mug", "wall"])

    def test_depth_ratios_do_NOT_survive_an_unknown_gain(self):
        """The correction. An earlier version claimed the rate ratio cancels g
        and so gave relative depth for free. g cancels in s_i/s_j, but a range
        needs s/g - 1, and that 1 puts it back."""
        implied = {}
        for guess in (0.55, 0.62, 0.70):
            got, _ = depth.ranges(
                [track("mug", 0.40), track("box", 0.70)], r=R, gain=guess)
            implied[guess] = got["box"]["range"] / got["mug"]["range"]
        self.assertAlmostEqual(implied[0.62], 1.75, delta=0.08)
        # The same data, a different assumed gain, a different answer.
        self.assertLess(implied[0.55], 1.6)
        self.assertGreater(implied[0.70], 2.2)

    def test_assuming_the_farthest_is_infinite_biases_ranges_LONG(self):
        """Measured, not reasoned: the assumed gain is too high, so the
        computed parallax is too small and everything reads as further away.
        An object at 0.40 m with nothing beyond 0.70 m in view reads as 1.2 m.
        """
        got, info = depth.ranges(
            [track("mug", 0.40), track("box", 0.70)], r=R)
        self.assertTrue(info["gain_assumed"])
        self.assertIn("LONG", info["warning"])
        self.assertGreater(got["mug"]["range"], 0.40 * 2,
                           "the long bias is not showing up")

    def test_a_truly_distant_object_makes_the_assumption_sound(self):
        """With something actually far in frame, the assumed gain is close to
        the real one and the ranges come back roughly right."""
        truth = {"mug": 0.45, "laptop": 0.90, "sky": 300.0}
        got, info = depth.ranges([track(k, v) for k, v in truth.items()], r=R)
        self.assertTrue(info["gain_assumed"])
        self.assertAlmostEqual(info["gain"], GAIN, delta=0.01)
        self.assertAlmostEqual(got["mug"]["range"], 0.45, delta=0.06)


class ItRefusesBadData(unittest.TestCase):
    def test_a_pan_too_small_to_fit_is_refused(self):
        t = depth.simulate("mug", 0.45, R, GAIN, FOV, [60, 61, 62])
        with self.assertRaises(depth.NotMeasurable) as cm:
            t.rate()
        self.assertIn("below", str(cm.exception))

    def test_a_stationary_arm_is_refused(self):
        t = depth.simulate("mug", 0.45, R, GAIN, FOV, [60, 60, 60])
        with self.assertRaises(depth.NotMeasurable):
            t.rate()

    def test_a_nonlinear_track_is_refused(self):
        """A correspondence that jumped between two objects, or an object that
        moved. Depth from a bad match is worse than no depth."""
        t = track("mug", 0.45)
        t.xs[3] += 260.0
        with self.assertRaises(depth.NotMeasurable) as cm:
            t.rate()
        self.assertIn("not linear", str(cm.exception))

    def test_a_rejected_track_does_not_sink_the_others(self):
        good = [track("mug", 0.45), track("wall", 4.0)]
        bad = track("ghost", 0.5)
        bad.xs[2] += 300.0
        got, info = depth.ranges(good + [bad], r=R, gain=GAIN)
        self.assertIn("ghost", info["rejected"])
        self.assertIn("mug", got)

    def test_no_usable_track_raises_rather_than_returning_empty(self):
        bad = track("ghost", 0.5)
        bad.xs[2] += 300.0
        with self.assertRaises(depth.NotMeasurable):
            depth.ranges([bad], r=R, gain=GAIN)

    def test_a_known_range_without_r_is_refused(self):
        with self.assertRaises(depth.NotMeasurable):
            depth.ranges([track("mug", 0.45)], known={"mug": 0.45})

    def test_ordering_with_nothing_usable_raises(self):
        bad = track("ghost", 0.5)
        bad.xs[2] += 300.0
        with self.assertRaises(depth.NotMeasurable):
            depth.ordering([bad])


class Projection(unittest.TestCase):
    def test_the_lens_is_treated_as_a_pinhole_not_as_linear_in_angle(self):
        """At the edge of a 52-degree lens the difference is a few per cent --
        the same order as the parallax being measured, so it is not
        ignorable."""
        t = depth.Track("x", FOV, 1280)
        for cx in (0, 320, 640, 960, 1280):
            t.add(60, cx)
        a = t.image_angles()
        self.assertAlmostEqual(a[2], 0.0, places=9)
        self.assertAlmostEqual(a[0], -FOV / 2, places=6)
        self.assertAlmostEqual(a[4], FOV / 2, places=6)
        # Linear-in-angle would put the quarter point at exactly FOV/4.
        self.assertNotAlmostEqual(a[3], FOV / 4, places=2)

    def test_the_estimate_holds_up_off_axis(self):
        """Objects are not all in the middle of the frame, and cos(eta) is
        why the correction is applied at all."""
        got, _ = depth.ranges([track("mug", 0.60, bearing_deg=18.0),
                               track("wall", 4.00)], r=R, gain=GAIN)
        self.assertAlmostEqual(got["mug"]["range"], 0.60, delta=0.08)
