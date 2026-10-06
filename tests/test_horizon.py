"""Finding level from how a base turn rolls the picture."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import horizon as hz                    # noqa: E402

W, H = 1280, 720


def affine_about_centre(roll_deg, dx, dy, scale=1.0):
    """The 2x3 OpenCV would fit for a roll + slide about the image centre."""
    a = math.radians(roll_deg)
    A = scale * np.array([[math.cos(a), -math.sin(a)],
                          [math.sin(a), math.cos(a)]])
    c = np.array([W / 2.0, H / 2.0])
    t = c + np.array([dx, dy]) - A @ c
    return np.column_stack([A, t])


def tilts(g, w_level, k=1.0, wrists=range(20, 177, 12), noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    ws = np.array(list(wrists), dtype=float)
    roll = g * np.sin(np.radians(k * (ws - w_level)))
    return ws, roll + rng.normal(0, noise, len(ws))


class ReadAtTheCentre(unittest.TestCase):
    def test_a_pure_roll_does_not_slide(self):
        """About the corner, a 5 degree roll is a ~70 px translation. Read at
        the centre it is no slide at all -- the one thing that must be right."""
        roll, dx, dy, s = hz.decompose(affine_about_centre(5.0, 0, 0), W, H)
        self.assertAlmostEqual(roll, 5.0, places=6)
        self.assertAlmostEqual(dx, 0.0, places=6)
        self.assertAlmostEqual(dy, 0.0, places=6)

    def test_roll_and_slide_come_apart(self):
        roll, dx, dy, s = hz.decompose(
            affine_about_centre(-3.0, 120.0, -8.0, 1.02), W, H)
        self.assertAlmostEqual(roll, -3.0, places=6)
        self.assertAlmostEqual(dx, 120.0, places=6)
        self.assertAlmostEqual(dy, -8.0, places=6)
        self.assertAlmostEqual(s, 1.02, places=6)


class FindingLevel(unittest.TestCase):
    def test_recovers_gain_and_level(self):
        ws, roll = tilts(g=1.0, w_level=60.0, noise=0.01)
        fit = hz.fit_roll(ws, roll)
        self.assertAlmostEqual(fit["g"], 1.0, delta=0.02)
        self.assertAlmostEqual(fit["w_level"], 60.0, delta=1.5)

    def test_under_travel_shows_up_as_a_small_gain(self):
        ws, roll = tilts(g=0.55, w_level=60.0, noise=0.01)
        self.assertAlmostEqual(hz.fit_roll(ws, roll)["g"], 0.55, delta=0.02)

    def test_the_wrist_gain_can_be_scanned(self):
        ws, roll = tilts(g=1.0, w_level=70.0, k=1.15, noise=0.005)
        fit = hz.fit_roll(ws, roll, k_grid=np.arange(0.8, 1.31, 0.01))
        self.assertAlmostEqual(fit["k"], 1.15, delta=0.03)
        self.assertAlmostEqual(fit["w_level"], 70.0, delta=2.0)

    def test_a_narrow_span_is_refused(self):
        """Over 30 degrees a sine is a line, and a line's zero crossing is not
        identifiable against its gain."""
        ws, roll = tilts(g=1.0, w_level=60.0, wrists=range(100, 131, 6))
        with self.assertRaises(hz.NotMeasurable):
            hz.fit_roll(ws, roll)

    def test_elevation_is_zero_at_level(self):
        fit = {"g": 1.0, "w_level": 60.0, "k": 1.0}
        self.assertEqual(hz.elevation(60.0, fit), 0.0)
        self.assertEqual(hz.elevation(150.0, fit), 90.0)


class WhatThe083Was(unittest.TestCase):
    def test_a_pitched_camera_slides_slow_with_no_under_travel(self):
        """The config's "under-travels 1.8x" reading, reproduced by geometry
        alone: a full-gain joint, a camera pitched 52 degrees."""
        g, e = 1.0, 52.0
        self.assertAlmostEqual(g * math.cos(math.radians(e)), 0.62, delta=0.01)

    def test_slide_and_roll_give_the_field_of_view(self):
        P = W / 52.0                                  # a 52 degree lens
        fit = {"g": 0.9, "w_level": 60.0, "k": 1.0}
        ws = np.arange(20, 177, 12, dtype=float)
        slides = [fit["g"] * math.cos(math.radians(hz.elevation(w, fit))) * P
                  for w in ws]
        got, rms = hz.px_per_deg(ws, slides, fit)
        self.assertAlmostEqual(W / got, 52.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
