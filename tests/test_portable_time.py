"""strftime's "%-I" crashes on Windows, where AVI runs; the Mac never sees it."""
import datetime as dt
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from avi_bot import numbers                                      # noqa: E402


class ClockTime(unittest.TestCase):
    def test_afternoon(self):
        self.assertEqual(numbers.clock_time(dt.datetime(2026, 10, 6, 16, 5)),
                         "4:05")

    def test_noon_and_midnight_are_twelve(self):
        self.assertEqual(numbers.clock_time(dt.datetime(2026, 10, 6, 12, 0),
                                            am_pm=True), "12:00 PM")
        self.assertEqual(numbers.clock_time(dt.datetime(2026, 10, 6, 0, 30),
                                            am_pm=True), "12:30 AM")

    def test_epoch_works_too(self):
        ts = dt.datetime(2026, 10, 6, 9, 7).timestamp()
        self.assertEqual(numbers.clock_time(ts), "9:07")


class NoPlatformOnlyDirectives(unittest.TestCase):
    def test_no_dash_flags_anywhere_in_the_robot(self):
        bad = []
        for path in list((ROOT / "avi_bot").rglob("*.py")) + list(
                (ROOT / "assistant").rglob("*.py")):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"%-[a-zA-Z]", line) and "numbers.py" not in str(path):
                    bad.append(f"{path.relative_to(ROOT)}:{i}")
        self.assertEqual(bad, [], "strftime dash flags crash on Windows")


if __name__ == "__main__":
    unittest.main()
