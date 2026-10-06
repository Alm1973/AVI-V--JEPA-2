"""base_yaw has 12-24 degrees of backlash; every target is reached going up."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.motion import backlash as bl                       # noqa: E402


class FromBelow(unittest.TestCase):
    def test_moving_up_is_one_command(self):
        self.assertEqual(bl.plan(80, 95, lo=20), [95.0])

    def test_moving_down_overshoots_then_comes_up(self):
        self.assertEqual(bl.plan(110, 70, lo=20), [46.0, 70.0])

    def test_unknown_position_counts_as_a_reversal(self):
        self.assertEqual(bl.plan(None, 70, lo=20), [46.0, 70.0])

    def test_the_run_up_stops_at_the_soft_limit(self):
        self.assertEqual(bl.plan(110, 30, lo=20), [20.0, 30.0])

    def test_the_run_up_covers_the_measured_backlash(self):
        """12 was tried and measured short; the constant must not drift back."""
        self.assertGreaterEqual(bl.YAW_BACKLASH_DEG, 24.0)


if __name__ == "__main__":
    unittest.main()


class RunUpFinishes(unittest.TestCase):
    def test_a_long_run_up_waits_longer(self):
        self.assertGreater(bl.travel_s(110, 46), bl.travel_s(70, 46))

    def test_unknown_start_waits_for_the_worst_case(self):
        self.assertGreaterEqual(bl.travel_s(None, 46), 180 / bl.SPEED_DEG_S)
