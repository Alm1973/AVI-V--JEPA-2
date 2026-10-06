"""Pixel + pose -> world direction, pinned to what the hardware measured."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import horizon as hz                    # noqa: E402
from avi_bot.perception import world as wd                      # noqa: E402

# [horizon] as measured 2026-10-06.
VIEW = wd.View(level_wrist=160.3, wrist_gain=1.03, yaw_gain=0.95,
               fov_deg=48.6)


def scene(view, yaw, wrist, n=60, seed=1):
    """Far points spread over what the camera sees at this pose."""
    rng = np.random.default_rng(seed)
    pts = []
    for _ in range(n):
        px, py = rng.uniform(100, 1180), rng.uniform(80, 640)
        pts.append(view.ray(px, py, yaw, wrist))
    return pts


def turn(view, yaw, wrist, delta=8.0):
    """roll (deg per deg) and slide (px per deg) for a base turn of delta,
    exactly as /probe_horizon reports them."""
    dirs = scene(view, yaw, wrist)
    a = [view.pixel(d, yaw, wrist) for d in dirs]
    b = [view.pixel(d, yaw + delta, wrist) for d in dirs]
    keep = [(p, q) for p, q in zip(a, b) if p is not None and q is not None]
    M = wd.similarity([p for p, _ in keep], [q for _, q in keep])
    roll, dx, dy, _ = hz.decompose(M, view.width, view.height)
    return roll / delta, dx / delta


class RoundTrip(unittest.TestCase):
    def test_pixel_and_direction_invert(self):
        for yaw, wrist in ((90, 160), (70, 120), (110, 60), (90, 30)):
            for px, py in ((640, 360), (100, 100), (1200, 650)):
                d = VIEW.ray(px, py, yaw, wrist)
                got = VIEW.pixel(d, yaw, wrist)
                self.assertAlmostEqual(got[0], px, places=6)
                self.assertAlmostEqual(got[1], py, places=6)

    def test_the_centre_is_the_camera_direction(self):
        az, el = VIEW.direction(640, 360, 100, 130)
        cam = VIEW.camera(100, 130)
        self.assertAlmostEqual(az, cam[0], places=6)
        self.assertAlmostEqual(el, cam[1], places=6)

    def test_aim_centres_a_direction(self):
        target = VIEW.direction(300, 500, 80, 120)
        yaw, wrist = VIEW.aim(*target)
        px, py = VIEW.pixel(target, yaw, wrist)
        self.assertAlmostEqual(px, 640, places=4)
        self.assertAlmostEqual(py, 360, places=4)


class MatchesTheHardware(unittest.TestCase):
    """The model must reproduce /probe_horizon's measurements, signs
    included -- that is what pins its conventions to the real arm."""

    def test_level_slides_left_and_does_not_roll(self):
        roll, slide = turn(VIEW, 90, 160.3)
        self.assertAlmostEqual(roll, 0.0, delta=0.02)
        self.assertLess(slide, -20)                    # measured -25.4 at 164

    def test_looking_down_rolls_negative(self):
        """Measured at wrist 104: roll -0.86/-0.79 per deg, slide -22."""
        roll, slide = turn(VIEW, 90, 104)
        self.assertLess(roll, -0.7)
        self.assertLess(slide, 0)

    def test_tall_looks_up_and_rolls_positive(self):
        """Measured at wrist 176: roll +0.26, the only positive level."""
        roll, _ = turn(VIEW, 90, 176)
        self.assertGreater(roll, 0.15)

    def test_past_the_nadir_the_slide_reverses(self):
        """Measured at wrist 20 and 32: slide +16 and +12."""
        _, slide = turn(VIEW, 90, 25)
        self.assertGreater(slide, 0)


class WhyTheFlatMapSplitObjects(unittest.TestCase):
    def test_one_object_from_two_tilts_is_one_direction(self):
        target = (12.0, -45.0)                    # on the desk, to the right
        seen = []
        for yaw, wrist in ((95, 125), (100, 110), (85, 118)):
            p = VIEW.pixel(target, yaw, wrist)
            self.assertIsNotNone(p)
            seen.append(VIEW.direction(p[0], p[1], yaw, wrist))
        for s in seen:
            self.assertLess(wd.angle_between(s, target), 1e-6)

    def test_azimuth_per_pixel_grows_as_the_view_drops(self):
        """The flat map's one constant: half a frame is the same yaw at any
        tilt. On the sphere, at 60 deg down it is about twice the azimuth."""
        def half_frame_azimuth(wrist):
            yaw = 90.0
            a = VIEW.direction(640, 360, yaw, wrist)[0]
            b = VIEW.direction(1280, 360, yaw, wrist)[0]
            return b - a
        level = half_frame_azimuth(160.3)
        down = half_frame_azimuth(160.3 - 60 / 1.03)
        self.assertGreater(down / level, 1.5)


class FromConfig(unittest.TestCase):
    def test_unmeasured_level_gives_no_view(self):
        cfg = type("C", (), {"raw": {}})()
        self.assertIsNone(wd.View.from_config(cfg))

    def test_measured_level_builds_one(self):
        cfg = type("C", (), {"raw": {"horizon": {
            "measured": True, "level_wrist": 160.3, "wrist_gain": 1.03,
            "yaw_gain": 0.95, "fov_deg": 48.6}}})()
        v = wd.View.from_config(cfg)
        self.assertAlmostEqual(v.camera(90, 160.3)[1], 0.0)


if __name__ == "__main__":
    unittest.main()


class TheMapUsesIt(unittest.TestCase):
    """RoomMap with a view files directions on the sphere."""

    def setUp(self):
        from avi_bot.perception import roommap as rm
        self.rm = rm

    def test_one_object_seen_from_two_tilts_is_one_place(self):
        m = self.rm.RoomMap(69.4, 30.5, clock=lambda: 1000.0, view=VIEW)
        target = (15.0, -40.0)
        for yaw, wrist in ((92, 130), (104, 112)):
            px, py = VIEW.pixel(target, yaw, wrist)
            m.observe("mug", px, py, yaw, wrist, conf=0.8, px_size=90)
        self.assertEqual(len([p for p in m.places if p.label == "mug"]), 1)

    def test_a_place_is_where_to_point_to_centre_it(self):
        m = self.rm.RoomMap(69.4, 30.5, view=VIEW)
        target = (-20.0, -30.0)
        px, py = VIEW.pixel(target, 80, 125)
        yaw, wrist = m.direction_of(px, py, 80, 125)
        cx, cy = VIEW.pixel(target, yaw, wrist)
        self.assertAlmostEqual(cx, 640, places=3)
        self.assertAlmostEqual(cy, 360, places=3)

    def test_the_geometry_survives_a_save(self):
        m = self.rm.RoomMap(69.4, 30.5, view=VIEW)
        back = self.rm.RoomMap.from_dict(m.to_dict())
        self.assertEqual(back.geometry, "world")
        self.assertEqual(back.view.to_dict(), VIEW.to_dict())

    def test_no_view_is_the_old_flat_map(self):
        m = self.rm.RoomMap(69.4, 30.5)
        self.assertEqual(m.geometry, "flat")
        self.assertEqual(m.direction_of(640, 360, 90, 140), (90.0, 140.0))
        self.assertEqual(self.rm.RoomMap.from_dict(m.to_dict()).geometry,
                         "flat")
