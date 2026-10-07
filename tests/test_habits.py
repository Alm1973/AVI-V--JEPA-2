"""The shape of his day, learned from presence sessions."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import habits as hb                    # noqa: E402


def at(day, h, m=0):
    """Oct 5 + day, h:m local. h may run past 24 for the small hours."""
    extra, h = divmod(h, 24)
    return datetime(2026, 10, 5 + day + extra, h, m).timestamp()


def evening(day, *spans):
    """[(start_h, start_m, end_h, end_m), ...] -> sessions."""
    return [(at(day, a, b), at(day, c, d)) for a, b, c, d in spans]


class Days(unittest.TestCase):
    def test_a_small_hours_session_belongs_to_the_evening_before(self):
        self.assertEqual(hb.day_of(at(0, 24, 40)), hb.day_of(at(0, 19)))
        self.assertAlmostEqual(hb.hour_in_day(at(0, 24, 30)), 24.5)

    def test_the_day_starts_at_four(self):
        self.assertEqual(hb.day_start_ts(at(0, 24, 40)), at(0, 4))
        self.assertEqual(hb.day_start_ts(at(0, 9)), at(0, 4))

    def test_one_or_two_days_are_not_a_habit(self):
        s = evening(0, (19, 0, 21, 0)) + evening(1, (19, 0, 21, 0))
        self.assertEqual(hb.summarize(s), {})


class Shape(unittest.TestCase):
    def three_days(self):
        out = []
        for d in range(3):
            # Sits down at 19:00, dinner 20:00-20:30, back until 23:00.
            out += evening(d, (19, 0, 20, 0), (20, 30, 23, 0))
        return out

    def test_the_start_is_the_first_sit_down_not_the_return_from_dinner(self):
        """The old median over every arrival said 20:30 here."""
        self.assertAlmostEqual(hb.summarize(self.three_days())["usual_start"],
                               19.0)

    def test_end_session_and_break(self):
        h = hb.summarize(self.three_days())
        self.assertEqual(h["days_seen"], 3)
        self.assertAlmostEqual(h["usual_end"], 23.0)
        self.assertAlmostEqual(h["usual_break_min"], 30.0)
        self.assertAlmostEqual(h["usual_session_min"], 105.0)  # 60 and 150

    def test_a_late_night_does_not_drag_the_start_to_the_morning(self):
        s = (evening(0, (23, 0, 25, 0)) + evening(1, (23, 30, 25, 0))
             + evening(2, (24, 15, 25, 30)))
        self.assertAlmostEqual(hb.summarize(s)["usual_start"], 23.5)

    def test_an_open_session_has_no_end_to_learn(self):
        s = self.three_days()
        s[-1] = (s[-1][0], None)
        h = hb.summarize(s)
        self.assertNotIn("usual_end", h)       # two finished days only

    def test_going_out_for_hours_is_not_a_break(self):
        s = []
        for d in range(3):
            s += evening(d, (9, 0, 12, 0), (19, 0, 22, 0))
        self.assertNotIn("usual_break_min", hb.summarize(s))


if __name__ == "__main__":
    unittest.main()
