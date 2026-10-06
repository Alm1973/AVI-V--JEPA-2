"""The one part of AVI that speaks without being spoken to.

Which makes it the part most able to be annoying, so the tests are mostly
about restraint: one announcement at a time, nothing during quiet or guest
mode, and stale items dropped rather than delivered late.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot import scheduler as sched_mod                    # noqa: E402
from avi_bot.memory import db as memdb                        # noqa: E402
from avi_bot.skills import modes as modes_mod                 # noqa: E402
from avi_bot.skills import timers as timers_mod               # noqa: E402


class Clock:
    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s
        return self.t


class Event:
    def __init__(self, summary, uid):
        self.summary = summary
        self.uid = uid


class FakeCalendar:
    def __init__(self, events=()):
        self.events = list(events)

    def due_reminders(self):
        out, self.events = self.events, []
        return out


def setup(with_calendar=None, with_modes=None):
    clock = Clock()
    store = memdb.Store(clock=clock)
    timers = timers_mod.Timers(store=store, clock=clock)
    s = sched_mod.Scheduler(timers=timers, store=store,
                            calendar=with_calendar, modes=with_modes,
                            clock=clock)
    return s, store, timers, clock


class Delivery(unittest.TestCase):
    def setUp(self):
        self.s, self.store, self.timers, self.clock = setup()
        self.addCleanup(self.store.close)

    def test_nothing_due_is_nothing_said(self):
        self.timers.set(300)
        self.assertIsNone(self.s.next_announcement())

    def test_a_due_timer_is_announced(self):
        self.timers.set(5, label="tea")
        self.clock.tick(6)
        a = self.s.next_announcement()
        self.assertEqual(a.kind, sched_mod.TIMER)
        self.assertIn("tea", a.text)

    def test_one_at_a_time(self):
        """Two announcements at once is noise, and the second gets ignored."""
        self.timers.set(5, label="tea")
        self.store.add_reminder(self.clock() + 5, "stand up")
        self.clock.tick(6)
        self.assertEqual(self.s.pending_count(), 2)
        a = self.s.next_announcement()
        self.assertIsNotNone(a)
        self.s.delivered_ok(a)
        b = self.s.next_announcement()
        self.assertIsNotNone(b)
        self.assertNotEqual(a.kind, b.kind)

    def test_soonest_first(self):
        self.store.add_reminder(self.clock() + 1, "first")
        self.store.add_reminder(self.clock() + 2, "second")
        self.clock.tick(5)
        self.assertEqual(self.s.next_announcement().text, "first")

    def test_it_repeats_until_delivery_is_confirmed(self):
        """A crash between speaking and marking should lose it loudly, not
        eat it silently."""
        self.timers.set(5)
        self.clock.tick(6)
        self.assertIsNotNone(self.s.next_announcement())
        self.assertIsNotNone(self.s.next_announcement())

    def test_confirmed_delivery_retires_it(self):
        self.timers.set(5)
        self.clock.tick(6)
        self.s.delivered_ok(self.s.next_announcement())
        self.assertIsNone(self.s.next_announcement())
        self.assertEqual(self.s.delivered, 1)

    def test_a_reminder_is_retired_in_the_store(self):
        self.store.add_reminder(self.clock() + 1, "call home")
        self.clock.tick(2)
        self.s.delivered_ok(self.s.next_announcement())
        self.assertEqual(self.store.due_reminders(), [])


class Staleness(unittest.TestCase):
    def setUp(self):
        self.s, self.store, self.timers, self.clock = setup()
        self.addCleanup(self.store.close)

    def test_an_old_timer_is_dropped_not_announced_late(self):
        """A timer from forty minutes ago is not news."""
        self.timers.set(5)
        self.clock.tick(sched_mod.STALE_AFTER_S + 60)
        self.assertIsNone(self.s.next_announcement())
        self.assertEqual(self.s.dropped, 1)
        self.assertEqual(self.s.delivered, 0, "counted a drop as a delivery")

    def test_a_dropped_item_does_not_come_back(self):
        self.timers.set(5)
        self.clock.tick(sched_mod.STALE_AFTER_S + 60)
        self.s.next_announcement()
        self.assertEqual(self.s.pending_count(), 0)

    def test_something_recent_is_still_delivered(self):
        self.timers.set(5)
        self.clock.tick(sched_mod.STALE_AFTER_S - 60)
        self.assertIsNotNone(self.s.next_announcement())


class SilentModes(unittest.TestCase):
    def test_quiet_mode_says_nothing(self):
        modes = modes_mod.Modes(clock=lambda: 0.0)
        s, store, timers, clock = setup(with_modes=modes)
        self.addCleanup(store.close)
        modes.clock = clock
        modes.quiet(10)
        timers.set(5)
        clock.tick(6)
        self.assertIsNone(s.next_announcement())
        self.assertEqual(s.pending_count(), 1, "lost it instead of holding it")

    def test_it_is_delivered_once_quiet_ends(self):
        """Time still passes: still-relevant items arrive, stale ones do not."""
        modes = modes_mod.Modes(clock=lambda: 0.0)
        s, store, timers, clock = setup(with_modes=modes)
        self.addCleanup(store.close)
        modes.clock = clock
        modes.quiet(2)
        timers.set(5)
        clock.tick(6)
        self.assertIsNone(s.next_announcement())
        clock.tick(130)                      # quiet expired, still fresh
        self.assertIsNotNone(s.next_announcement())

    def test_guest_mode_says_nothing(self):
        modes = modes_mod.Modes()
        s, store, timers, clock = setup(with_modes=modes)
        self.addCleanup(store.close)
        modes.guest_mode(True)
        timers.set(5)
        clock.tick(6)
        self.assertIsNone(s.next_announcement())

    def test_sleep_says_nothing(self):
        modes = modes_mod.Modes()
        s, store, timers, clock = setup(with_modes=modes)
        self.addCleanup(store.close)
        modes.sleep()
        timers.set(5)
        clock.tick(6)
        self.assertIsNone(s.next_announcement())


class CalendarWarnings(unittest.TestCase):
    def test_an_event_warning_is_announced(self):
        cal = FakeCalendar([Event("Chemistry", "chem-1")])
        s, store, timers, clock = setup(with_calendar=cal)
        self.addCleanup(store.close)
        a = s.next_announcement()
        self.assertEqual(a.kind, sched_mod.EVENT)
        self.assertIn("Chemistry", a.text)
        self.assertIn("ten minutes", a.text)

    def test_a_warning_survives_a_poll_that_did_not_deliver_it(self):
        """Calendar.due_reminders() dedups ON READ, and due() is polled by
        design, so without buffering any warning that was not the soonest item
        vanished silently. This is the regression test for that."""
        cal = FakeCalendar([Event("Chemistry", "chem-1")])
        s, store, timers, clock = setup(with_calendar=cal)
        self.addCleanup(store.close)
        timers.set(1, label="tea")
        clock.tick(2)
        first = s.next_announcement()          # the timer, sooner
        self.assertEqual(first.kind, sched_mod.TIMER)
        s.delivered_ok(first)
        second = s.next_announcement()
        self.assertIsNotNone(second, "the calendar warning was lost")
        self.assertEqual(second.kind, sched_mod.EVENT)

    def test_a_warning_is_not_repeated_after_delivery(self):
        cal = FakeCalendar([Event("Chemistry", "chem-1")])
        s, store, timers, clock = setup(with_calendar=cal)
        self.addCleanup(store.close)
        s.delivered_ok(s.next_announcement())
        self.assertIsNone(s.next_announcement())

    def test_a_calendar_outage_is_not_an_alarm(self):
        class Broken:
            def due_reminders(self):
                raise OSError("no network")
        s, store, timers, clock = setup(with_calendar=Broken())
        self.addCleanup(store.close)
        self.assertIsNone(s.next_announcement())

    def test_announcements_fit_the_speech_cap(self):
        from avi_bot.brain import speech
        cal = FakeCalendar([Event("Dinner with parents", "d-1")])
        s, store, timers, clock = setup(with_calendar=cal)
        self.addCleanup(store.close)
        timers.set(5, label="the pasta")
        store.add_reminder(clock() + 1, "take the bins out")
        clock.tick(6)
        seen = 0
        while True:
            a = s.next_announcement()
            if a is None:
                break
            self.assertLessEqual(len(a.text.split()), speech.MAX_WORDS, a.text)
            s.delivered_ok(a)
            seen += 1
            if seen > 5:
                break
        self.assertGreaterEqual(seen, 3)


if __name__ == "__main__":
    unittest.main()
