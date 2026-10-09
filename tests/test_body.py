"""AVI's body in 3D (motion/body.py): where it is, where it looks, and what
a move should do to the picture."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.motion import body as body_mod, poses             # noqa: E402
from avi_bot.perception import verify                          # noqa: E402


def body(**kw):
    return body_mod.Body.from_config(poses.load())


class TheRulerAnchor(unittest.TestCase):
    """Shaurya measured the lens at TALL: 25.0 cm out, 34.4 cm up."""

    def test_tall_lands_on_the_ruler_numbers(self):
        b = body()
        x, y, z = b.points(poses.get_pose("TALL"))["lens"]
        self.assertAlmostEqual(math.hypot(x, y), 0.25, places=3)
        self.assertAlmostEqual(z, 0.344, places=3)

    def test_tall_looks_where_horizon_measured(self):
        # level is wrist 160.3 at TALL; TALL's 176 is ~16 degrees above it
        self.assertAlmostEqual(body().elevation(poses.get_pose("TALL")),
                               1.03 * (176.0 - 160.3), places=1)

    def test_the_lengths_are_priors_and_it_says_so(self):
        self.assertFalse(body().measured)

    def test_the_fit_is_exact_for_any_priors(self):
        for h, u, l in ((0.08, 0.12, 0.05), (0.12, 0.2, 0.08)):
            b = body_mod.Body.fit(0.25, 0.344, 160.3, base_height_m=h,
                                  upper_arm_m=u, lens_m=l)
            x, _, z = b.points(body_mod.TALL)["lens"]
            self.assertAlmostEqual(x, 0.25, places=4)
            self.assertAlmostEqual(z, 0.344, places=4)


class WhereItLooks(unittest.TestCase):
    def test_the_seat_view_looks_down_at_the_seat(self):
        self.assertLess(body().elevation(poses.get_pose("ATTENTIVE")), -10)

    def test_turning_the_base_turns_the_whole_arm(self):
        b = body()
        p = poses.get_pose("ATTENTIVE")
        right = dict(p, base_yaw=120.0)
        x, y, _ = b.points(right)["lens"]
        self.assertLess(y, 0, "+base_yaw turns right, which is -y")
        self.assertAlmostEqual(b.azimuth(right), 0.95 * 30, places=3)
        self.assertAlmostEqual(math.hypot(x, y),
                               math.hypot(*b.points(p)["lens"][:2]),
                               delta=1e-3)

    def test_desk_facing_poses_keep_clear_of_the_desk(self):
        b = body()
        for name in ("TALL", "ATTENTIVE", "HOME", "OBSERVE"):
            self.assertTrue(b.safe(poses.get_pose(name)), name)

    def test_rest_is_a_shape_it_does_not_describe(self):
        """Folded flat, the linear chain puts the wrist under the desk --
        the model is wrong there, and choreo treats 'unsafe at the start'
        as 'out of the model's range' rather than trusting it."""
        self.assertFalse(body().safe(poses.get_pose("REST")))


class WhatAMoveShouldDo(unittest.TestCase):
    def setUp(self):
        self.b = body()
        self.p = poses.get_pose("ATTENTIVE")

    def test_base_and_wrist_as_before(self):
        for joint, d, want in (("base_yaw", 8, "right"),
                               ("base_yaw", -8, "left"),
                               ("wrist", 8, "up"), ("wrist", -8, "down")):
            to = dict(self.p, **{joint: self.p[joint] + d})
            self.assertEqual(self.b.direction(self.p, to), want)
            self.assertEqual(verify.dominant(self.p, to), want)

    def test_shoulder_and_elbow_only_once_proven(self):
        unproven = body_mod.Body.fit(0.25, 0.344, 160.3)
        to = dict(self.p, shoulder=self.p["shoulder"] + 6)
        self.assertIsNone(unproven.direction(self.p, to))
        self.assertEqual(unproven.direction(self.p, to, reach=True), "down")
        to = dict(self.p, elbow=self.p["elbow"] - 6)
        self.assertEqual(unproven.direction(self.p, to, reach=True), "up")

    def test_proven_on_the_robot(self):
        """POST /babble 2026-10-09: the model's shoulder/elbow direction
        matched the picture 15/16 (ORB) and 12/13 (V-JEPA)."""
        self.assertTrue(self.b.reach_judged)
        to = dict(self.p, shoulder=self.p["shoulder"] + 6)
        self.assertEqual(self.b.direction(self.p, to), "down")
        self.assertEqual(verify.dominant(self.p, to), "down")
        to = dict(self.p, elbow=self.p["elbow"] - 6)
        self.assertEqual(verify.dominant(self.p, to), "up")

    def test_a_small_move_expects_nothing(self):
        to = dict(self.p, wrist=self.p["wrist"] + 1)
        self.assertIsNone(self.b.direction(self.p, to))

    def test_partial_poses_fall_back_to_the_old_rule(self):
        self.assertEqual(verify.dominant({"base_yaw": 90, "wrist": 120},
                                         {"base_yaw": 100, "wrist": 120}),
                         "right")


if __name__ == "__main__":
    unittest.main()
