"""The search behind a look: where to try, and what counts as found."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import look_check as lc                # noqa: E402

LIMITS = ((20.0, 160.0), (20.0, 176.0))


class FakeCamera:
    """Sees `label` only when the arm points within 3 degrees of `at`."""

    def __init__(self, label, at, conf=0.6):
        self.label, self.at, self.conf = label, at, conf
        self.pose, self.visited = None, []

    def move(self, yaw, wrist):
        self.pose = (yaw, wrist)
        self.visited.append(self.pose)

    def detect(self):
        near = (abs(self.pose[0] - self.at[0]) <= 3
                and abs(self.pose[1] - self.at[1]) <= 3)
        kept = [{"label": "mug", "conf": 0.9, "cx": 100, "cy": 100,
                 "w": 50, "h": 50}]
        if near:
            kept.append({"label": self.label, "conf": self.conf, "cx": 640,
                         "cy": 360, "w": 300, "h": 200})
        return kept, 1280, 720


class Search(unittest.TestCase):
    def test_at_the_aim_is_found_without_moving_on(self):
        cam = FakeCamera("laptop", (121.5, 145.0))
        got = lc.search("laptop", (121.5, 145.0), cam.move, cam.detect,
                        *LIMITS)
        self.assertEqual(got["offset"], (0.0, 0.0))
        self.assertEqual(len(cam.visited), 1)

    def test_fifteen_left_like_the_mug(self):
        cam = FakeCamera("mug2", (89.5, 144.5))
        got = lc.search("mug2", (104.5, 144.5), cam.move, cam.detect,
                        *LIMITS)
        self.assertEqual(got["offset"], (-15.0, 0.0))
        self.assertEqual(got["pose"], (89.5, 144.5))

    def test_low_and_close_is_found_by_the_wrist(self):
        cam = FakeCamera("laptop", (121.5, 133.0))
        got = lc.search("laptop", (121.5, 145.0), cam.move, cam.detect,
                        *LIMITS)
        self.assertEqual(got["offset"], (0.0, -12.0))

    def test_nowhere_near_is_none_after_every_stop(self):
        cam = FakeCamera("laptop", (40.0, 100.0))
        self.assertIsNone(lc.search("laptop", (121.5, 145.0), cam.move,
                                    cam.detect, *LIMITS))
        self.assertEqual(len(cam.visited), len(lc.STOPS))

    def test_stops_respect_the_limits(self):
        cam = FakeCamera("laptop", (0.0, 0.0))
        lc.search("laptop", (155.0, 170.0), cam.move, cam.detect, *LIMITS)
        for yaw, wrist in cam.visited:
            self.assertTrue(20 <= yaw <= 160 and 20 <= wrist <= 176)

    def test_another_label_is_not_it(self):
        cam = FakeCamera("laptop", (0.0, 0.0))
        got = lc.search("mug", (90.0, 140.0), cam.move, cam.detect, *LIMITS)
        self.assertEqual(got["hit"]["label"], "mug")   # the mug is in view
        self.assertIsNone(lc.best([{"label": "cup", "conf": 0.9}], "mug"))

    def test_no_picture_is_not_found(self):
        got = lc.search("mug", (90.0, 140.0), lambda y, w: None,
                        lambda: None, *LIMITS)
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
