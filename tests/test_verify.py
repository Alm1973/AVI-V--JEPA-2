"""Judging a move from the picture: ORB slide and V-JEPA token shift."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import verify as vf                    # noqa: E402


def grid(seed=0, h=16, w=16, d=32):
    return np.random.default_rng(seed).normal(size=(h, w, d))


def smooth(g, k=5):
    """Neighbouring tokens alike, as in a real image's features."""
    out = np.array(g, dtype=float)
    for axis in (0, 1):
        acc = np.zeros_like(out)
        for o in range(-(k // 2), k // 2 + 1):
            acc += np.roll(out, o, axis=axis)
        out = acc / k
    return out


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


def positional(g, strength=1.0, seed=7):
    """V-JEPA's tokens carry their grid position: a fixed per-position
    vector, the same in every frame, about as strong as the content (the
    robot's moved frames still scored ~0.7 raw at zero shift)."""
    return np.random.default_rng(seed).normal(size=g.shape) * strength


class JepaTokens(unittest.TestCase):
    def test_the_position_signal_does_not_hide_a_tilt(self):
        """The robot's 'up': raw matching peaked at no-shift because both
        frames share the position signal. The difference cancels it."""
        c = smooth(grid()) * 5.0          # unit-ish content, like real tokens
        p = positional(c)
        got = vf.token_shift(c + p, slid(c, 0, 6) + p)
        self.assertAlmostEqual(got["dy"], 6, delta=0.5)
        self.assertAlmostEqual(got["dx"], 0, delta=0.5)
        self.assertTrue(got["confident"])

    def test_the_position_signal_does_not_bend_a_turn(self):
        """The difference method turned a 3-token pan into a diagonal on
        the robot; the self-referenced score must keep it on its axis."""
        c = smooth(grid()) * 5.0
        p = positional(c)
        for dx, dy in ((3, 0), (-3, 0), (0, -6), (2, 4)):
            with self.subTest((dx, dy)):
                got = vf.token_shift(c + p, slid(c, dx, dy) + p)
                self.assertAlmostEqual(got["dx"], dx, delta=0.5)
                self.assertAlmostEqual(got["dy"], dy, delta=0.5)

    def test_one_axis_search_stays_on_its_axis(self):
        c = smooth(grid()) * 5.0
        p = positional(c)
        got = vf.token_shift(c + p, slid(c, -4, 0) + p, axis="x")
        self.assertAlmostEqual(got["dx"], -4, delta=0.5)
        self.assertLessEqual(abs(got["dy"]), 1.0)
        got = vf.token_shift(c + p, slid(c, 0, 5) + p, axis="y")
        self.assertAlmostEqual(got["dy"], 5, delta=0.5)

    def test_an_unmoved_scene_is_still(self):
        g = smooth(grid()) + positional(grid())
        got = vf.token_shift(g, g + 0.02 * grid(seed=3))
        self.assertTrue(got["still"])

    def test_finds_the_shift(self):
        g = grid()
        for dx, dy in ((-3, 0), (2, 0), (0, 2), (-1, -2)):
            with self.subTest((dx, dy)):
                got = vf.token_shift(g, slid(g, dx, dy))
                self.assertAlmostEqual(got["dx"], dx, delta=0.5)
                self.assertAlmostEqual(got["dy"], dy, delta=0.5)

    def test_a_tilt_bigger_than_six_rows_is_found(self):
        """The robot's 12-degree 'up' was 6.7 rows: outside the old +-6."""
        g = grid()
        got = vf.token_shift(g, slid(g, 0, 7))
        self.assertAlmostEqual(got["dy"], 7, delta=0.5)
        self.assertFalse(got["edge"])

    def test_beyond_the_search_is_flagged_not_trusted(self):
        """Real features are smooth, so the match climbs toward a shift the
        search cannot reach and peaks on its edge."""
        g = smooth(grid())
        got = vf.token_shift(g, slid(g, 0, 7), max_shift=4)
        self.assertTrue(got["edge"])
        self.assertFalse(got["confident"])

    def test_a_real_shift_is_confident(self):
        g = smooth(grid())
        self.assertTrue(vf.token_shift(g, slid(g, -3, 0))["confident"])

    def test_no_motion_is_zero(self):
        g = grid()
        got = vf.token_shift(g, g + 0.01 * grid(seed=5))
        self.assertEqual((round(got["dx"]), round(got["dy"])), (0, 0))
        self.assertTrue(got["still"])


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
