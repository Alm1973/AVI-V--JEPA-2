"""base_yaw has 12-24 degrees of backlash; every target is reached going up."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.motion import backlash as bl                       # noqa: E402


class FromBelow(unittest.TestCase):
    def test_moving_up_with_the_slack_taken_is_one_command(self):
        self.assertEqual(bl.plan(80, 95, lo=20, settled=True), [95.0])

    def test_moving_up_by_more_than_the_backlash_is_one_command(self):
        self.assertEqual(bl.plan(40, 95, lo=20), [95.0])

    def test_a_short_move_up_from_an_unknown_state_gets_a_run_up(self):
        """The /find miss: parked at 90 going down, then 10.9 up."""
        got = bl.plan(90, 100.9, lo=20)
        self.assertEqual(len(got), 2)
        self.assertAlmostEqual(100.9 - got[0], bl.YAW_BACKLASH_DEG)

    def test_moving_down_overshoots_then_comes_up(self):
        self.assertEqual(bl.plan(110, 70, lo=20), [70.0 - bl.YAW_BACKLASH_DEG, 70.0])

    def test_unknown_position_counts_as_a_reversal(self):
        self.assertEqual(bl.plan(None, 70, lo=20), [70.0 - bl.YAW_BACKLASH_DEG, 70.0])

    def test_the_run_up_stops_at_the_soft_limit(self):
        self.assertEqual(bl.plan(110, 30, lo=20), [20.0, 30.0])

    def test_the_run_up_covers_the_measured_backlash(self):
        """13.1 measured at level, up to ~16 effective looking down; 12 was
        tried and measured short. The constant must not drift back."""
        self.assertGreaterEqual(bl.YAW_BACKLASH_DEG, 16.0)


if __name__ == "__main__":
    unittest.main()


class RunUpFinishes(unittest.TestCase):
    def test_a_long_run_up_waits_longer(self):
        self.assertGreater(bl.travel_s(110, 46), bl.travel_s(70, 46))

    def test_unknown_start_waits_for_the_worst_case(self):
        self.assertGreaterEqual(bl.travel_s(None, 46), 180 / bl.SPEED_DEG_S)


class Settled(unittest.TestCase):
    def test_a_full_run_up_settles(self):
        s = bl.Slack(24)
        for cur, tgt in zip([110, 46], [46, 70]):
            s.command(cur, tgt)
        self.assertTrue(s.settled)

    def test_a_run_up_cut_short_by_the_limit_does_not(self):
        """The survey's first column: from 154 down to soft_min 20, then up
        0.7 to 20.7."""
        s = bl.Slack(24)
        s.command(154, 20.0)
        self.assertFalse(s.command(20.0, 20.7))

    def test_upward_steps_accumulate_until_settled(self):
        s = bl.Slack(24)
        s.command(154, 20.0)
        s.command(20.0, 20.7)
        self.assertFalse(s.command(20.7, 34.0))          # 14 of 24
        self.assertTrue(s.command(34.0, 47.4))           # 27.4

    def test_someone_else_moving_the_joint_voids_it(self):
        s = bl.Slack(24)
        s.command(40, 70)
        self.assertTrue(s.settled)
        self.assertFalse(s.command(90, 95))      # it was at 90, not 70
