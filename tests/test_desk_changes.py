"""What changed on the desk -- and the one rule that keeps it honest: an object
has only disappeared if the new scan looked where it used to be."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import desk_changes as dc               # noqa: E402

HALF = (37.6, 15.2)                  # half a frame, in yaw and wrist degrees
DESK = [(y, w) for y in (40, 70, 100, 130) for w in (140, 155)]


def seen(label, yaw, wrist, n=3, conf=0.6):
    return [(label, yaw + 0.5 * i, wrist, conf) for i in range(n)]


class Snapshot(unittest.TestCase):
    def test_repeated_sightings_become_one_object(self):
        snap = dc.snapshot(seen("laptop", 94, 153) + seen("pen", 34, 139))
        self.assertEqual(sorted(o["label"] for o in snap), ["laptop", "pen"])
        self.assertEqual([o["hits"] for o in snap if o["label"] == "pen"], [3])

    def test_a_single_flicker_is_not_an_object(self):
        snap = dc.snapshot(seen("laptop", 94, 153) + [("wallet", 60, 150, 0.5)])
        self.assertNotIn("wallet", [o["label"] for o in snap])

    def test_weak_detections_are_ignored(self):
        self.assertEqual(dc.snapshot(seen("book", 100, 150, conf=0.2)), [])


class Changes(unittest.TestCase):
    def setUp(self):
        self.before = dc.snapshot(seen("laptop", 94, 153) + seen("pen", 34, 139)
                                  + seen("backpack", 38, 144))

    def test_nothing_changed(self):
        d = dc.diff(self.before, self.before, DESK, HALF)
        self.assertEqual(sorted(d["stayed"]), ["backpack", "laptop", "pen"])
        self.assertEqual(d["appeared"] + d["disappeared"] + d["moved"], [])

    def test_something_appeared(self):
        after = self.before + dc.snapshot(seen("mug", 97, 156))
        d = dc.diff(self.before, after, DESK, HALF)
        self.assertEqual([a["label"] for a in d["appeared"]], ["mug"])

    def test_something_disappeared_where_the_scan_looked(self):
        after = [o for o in self.before if o["label"] != "backpack"]
        d = dc.diff(self.before, after, DESK, HALF)
        self.assertEqual([a["label"] for a in d["disappeared"]], ["backpack"])

    def test_not_looking_is_not_absence(self):
        """The rule. A scan that only covered the right side of the desk has
        no opinion about the backpack on the left."""
        right_only = [(y, w) for y in (110, 130) for w in (140, 155)]
        after = [o for o in self.before if o["label"] != "backpack"]
        d = dc.diff(self.before, after, right_only, HALF)
        self.assertEqual(d["disappeared"], [])
        self.assertIn("backpack", d["unchecked"])

    def test_something_moved(self):
        after = [dict(o, yaw=o["yaw"] + 30) if o["label"] == "pen" else o
                 for o in self.before]
        d = dc.diff(self.before, after, DESK, HALF)
        self.assertEqual([m["label"] for m in d["moved"]], ["pen"])
        self.assertAlmostEqual(d["moved"][0]["by_deg"], 30.0, delta=0.5)

    def test_jitter_is_not_movement(self):
        after = [dict(o, yaw=o["yaw"] + 3) for o in self.before]
        d = dc.diff(self.before, after, DESK, HALF)
        self.assertEqual(d["moved"], [])


class TheLog(unittest.TestCase):
    def test_first_scan_has_nothing_to_compare(self):
        log = dc.DeskLog(clock=lambda: 1000.0)
        self.assertIsNone(log.record(dc.snapshot(seen("pen", 34, 139)),
                                     DESK, HALF))

    def test_changes_are_kept_with_whether_he_was_here(self):
        """Over days, "the backpack goes when he leaves" is this table."""
        log = dc.DeskLog(clock=lambda: 1000.0)
        log.record(dc.snapshot(seen("backpack", 38, 144)), DESK, HALF,
                   here=True, at=1000)
        ch = log.record([], DESK, HALF, here=False, at=2000)
        self.assertEqual([d["label"] for d in ch["disappeared"]], ["backpack"])
        h = log.history("backpack")
        self.assertEqual(h[0]["kind"], "disappeared")
        self.assertIs(h[0]["here"], False)

    def test_dev_is_the_default_store(self):
        self.assertEqual(dc.PROFILES["dev"], "desk-dev.db")


if __name__ == "__main__":
    unittest.main()
