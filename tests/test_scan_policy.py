"""Scheduled desk scans: off by default, never while he is there."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import scan_policy as sp                    # noqa: E402

T = 1_791_300_000.0


class Due(unittest.TestCase):
    def test_off_unless_switched_on(self):
        self.assertFalse(sp.due(T, None, 0, here=False, hour=14)[0])

    def test_never_while_he_is_there(self):
        ok, why = sp.due(T, None, 60, here=True, hour=14)
        self.assertFalse(ok)
        self.assertIn("at the desk", why)

    def test_not_at_night(self):
        self.assertFalse(sp.due(T, None, 60, here=False, hour=2)[0])

    def test_once_per_interval(self):
        self.assertFalse(sp.due(T, T - 30 * 60, 60, here=False, hour=14)[0])
        self.assertTrue(sp.due(T, T - 61 * 60, 60, here=False, hour=14)[0])

    def test_first_scan_is_due_at_once(self):
        self.assertTrue(sp.due(T, None, 60, here=False, hour=14)[0])


if __name__ == "__main__":
    unittest.main()
