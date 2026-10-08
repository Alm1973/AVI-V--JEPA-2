"""Judging a move from the picture: ORB slide and V-JEPA token shift."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import verify as vf                    # noqa: E402


def grid(seed=0, h=16, w=16, d=32):
    return np.random.default_rng(seed).normal(size=(h, w, d))


def slid(g, dx, dy):
    """The same scene with its content moved dx right, dy down (tokens);
    new content (fresh noise) comes in at the uncovered edges."""
    out = grid(seed=99, h=g.shape[0], w=g.shape[1], d=g.shape[2])
    h, w = g.shape[:2]
    for y in range(h):
        for x in range(w):
            sy, sx = y - dy, x - dx
            if 0 <= sy < h and 0 <= sx < w:
                out[y, x] = g[sy, sx]
    return out


class WhichWayTheCameraWent(unittest.TestCase):
    def test_content_left_means_the_camera_turned_right(self):
        self.assertEqual(vf.camera_direction(-200, 5), "right")
        self.assertEqual(vf.camera_direction(200, 5), "left")

    def test_content_down_means_it_tilted_up(self):
        self.assertEqual(vf.camera_direction(3, 150), "up")
        self.assertEqual(vf.camera_direction(3, -150), "down")

    def test_a_still_picture_is_none(self):
        self.assertEqual(vf.camera_direction(4, -3), "none")


class JepaTokens(unittest.TestCase):
    def test_finds_the_shift(self):
        g = grid()
        for dx, dy in ((-3, 0), (2, 0), (0, 2), (-1, -2)):
            with self.subTest((dx, dy)):
                got = vf.token_shift(g, slid(g, dx, dy))
                self.assertAlmostEqual(got["dx"], dx, delta=0.5)
                self.assertAlmostEqual(got["dy"], dy, delta=0.5)

    def test_no_motion_is_zero(self):
        g = grid()
        got = vf.token_shift(g, g + 0.01 * grid(seed=5))
        self.assertEqual((round(got["dx"]), round(got["dy"])), (0, 0))


class Verdict(unittest.TestCase):
    def test_both_judges_agree_with_the_command(self):
        v = vf.judge("right", orb_px=(-240, 4), jepa_tokens=(-3, 0))
        self.assertTrue(v["agrees"])
        self.assertEqual(v["votes"], {"orb": "right", "jepa": "right"})
        self.assertIsNone(vf.sentence(v))

    def test_a_dead_servo_is_said(self):
        v = vf.judge("left", orb_px=(2, 1), jepa_tokens=(0, 0))
        self.assertFalse(v["agrees"])
        self.assertEqual(vf.sentence(v),
                         "I tried to look left, but the camera didn't move.")

    def test_the_wrong_way_is_said(self):
        v = vf.judge("left", orb_px=(-200, 0), jepa_tokens=(-2.5, 0))
        self.assertIn("went right", vf.sentence(v))

    def test_blind_is_not_a_failure(self):
        v = vf.judge("up")
        self.assertFalse(v["judged"])
        self.assertIsNone(vf.sentence(v))

    def test_one_judge_is_enough(self):
        self.assertTrue(vf.judge("up", orb_px=(5, 180))["agrees"])

    def test_move_joints_is_judged_by_its_larger_change(self):
        a = {"base_yaw": 90, "wrist": 150}
        self.assertEqual(vf.dominant(a, {"base_yaw": 75, "wrist": 152}),
                         "left")
        self.assertEqual(vf.dominant(a, {"base_yaw": 91, "wrist": 165}), "up")
        self.assertIsNone(vf.dominant(a, {"base_yaw": 91, "wrist": 151}))


if __name__ == "__main__":
    unittest.main()
