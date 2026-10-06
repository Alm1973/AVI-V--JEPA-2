"""A break is offered at a natural pause after a long stretch -- never on a
stopwatch, never mid-activity, never as a nag."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.skills import breaks as br                        # noqa: E402

T0 = 1_791_295_200.0
FIFTY = br.focus_mod.BREAK_AFTER_S


class FakePresence:
    def __init__(self, here=True, since=T0):
        self.here, self.since = here, since


def coach(**kw):
    p = FakePresence(**kw)
    return br.BreakCoach(p, clock=lambda: T0), p


class TheClockOnlyMakesHimEligible(unittest.TestCase):
    def test_no_offer_before_fifty_minutes(self):
        c, _ = coach()
        self.assertIsNone(c.on_breakpoint("change", now=T0 + FIFTY - 60))

    def test_no_offer_without_a_breakpoint_however_long(self):
        """There is no time-only path into on_breakpoint at all -- the method
        is only ever called when change.py reports one."""
        c, _ = coach()
        self.assertEqual(c.status(now=T0 + 3 * FIFTY)["waiting_for_a_pause"],
                         True)

    def test_a_settled_pause_after_fifty_minutes_is_the_moment(self):
        c, _ = coach()
        self.assertEqual(c.on_breakpoint("change", now=T0 + FIFTY + 1),
                         br.BREAK)


class OnlyANaturalPause(unittest.TestCase):
    def test_motion_that_never_settled_is_not_a_pause(self):
        """change-forced is someone mid-activity."""
        c, _ = coach()
        self.assertIsNone(c.on_breakpoint("change-forced", now=T0 + FIFTY + 1))


class NotANag(unittest.TestCase):
    def test_one_offer_then_quiet_for_another_stretch(self):
        c, _ = coach()
        t = T0 + FIFTY + 1
        self.assertEqual(c.on_breakpoint("change", now=t), br.BREAK)
        for k in range(1, 30):
            self.assertIsNone(c.on_breakpoint("change", now=t + k * 60))

    def test_a_long_session_can_be_offered_again(self):
        c, _ = coach()
        t = T0 + FIFTY + 1
        c.on_breakpoint("change", now=t)
        self.assertEqual(c.on_breakpoint("change", now=t + FIFTY + 1),
                         br.BREAK)

    def test_a_new_session_starts_the_clock_again(self):
        c, p = coach()
        c.on_breakpoint("change", now=T0 + FIFTY + 1)
        p.since = T0 + 2 * FIFTY                     # he left and came back
        self.assertIsNone(c.on_breakpoint("change", now=p.since + 600))
        self.assertEqual(c.on_breakpoint("change", now=p.since + FIFTY + 1),
                         br.BREAK)


class OnlyWhenHeIsThere(unittest.TestCase):
    def test_nobody_at_the_desk_gets_no_offer(self):
        c, _ = coach(here=False, since=None)
        self.assertIsNone(c.on_breakpoint("change", now=T0 + 5 * FIFTY))
        self.assertIsNone(c.status()["eligible_in_min"])


if __name__ == "__main__":
    unittest.main()
