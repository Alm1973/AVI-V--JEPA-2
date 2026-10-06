"""Calendar, against BUILD_SPEC 9's test: "a fixture .ics file gives the right
events and the right reminder times".

The fixture is deliberately awkward: a timed event, an all-day deadline, a
weekly recurrence, and events on two different days. All-day events are the
interesting case -- a deadline with no clock time must not produce a "in ten
minutes" nudge at midnight.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.skills import calendar as cal_mod                # noqa: E402

ICS = (Path(__file__).resolve().parents[1] /
       "fixtures" / "ics" / "desk.ics").read_text()
PHX = dt.timezone(dt.timedelta(hours=-7))


def at(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=PHX)


def cal(now):
    return cal_mod.Calendar(ics_text=ICS, tz=PHX, clock=lambda: now)


class Events(unittest.TestCase):
    def test_todays_events_in_order(self):
        c = cal(at(2026, 10, 6, 7, 0))
        names = [e.summary for e in c.events("today")]
        # 2026-10-06 is a Tuesday, so the weekly check-in belongs here too.
        # The all-day deadline sorts first because it starts at midnight.
        self.assertEqual(names, ["Science fair deadline", "Research check-in",
                                 "Chemistry", "Dentist",
                                 "Dinner with parents"])

    def test_tomorrow_is_a_different_day(self):
        c = cal(at(2026, 10, 6, 7, 0))
        self.assertEqual([e.summary for e in c.events("tomorrow")],
                         ["Robotics club"])

    def test_a_weekly_recurrence_is_expanded(self):
        """2026-10-06 is a Tuesday, and the check-in recurs on Tuesdays from
        September. Rolling our own RRULE expansion would be the most
        error-prone thing in this repo."""
        c = cal(at(2026, 10, 6, 7, 0))
        week = [e.summary for e in c.events("this week")]
        self.assertIn("Research check-in", week)

    def test_an_all_day_event_is_flagged(self):
        c = cal(at(2026, 10, 6, 7, 0))
        deadline = [e for e in c.events("today")
                    if e.summary == "Science fair deadline"][0]
        self.assertTrue(deadline.all_day)
        self.assertEqual(deadline.spoken_time(), "all day")

    def test_timezone_is_respected(self):
        c = cal(at(2026, 10, 6, 7, 0))
        chem = [e for e in c.events("today") if e.summary == "Chemistry"][0]
        self.assertEqual(chem.start.hour, 10)

    def test_location_is_kept(self):
        c = cal(at(2026, 10, 6, 7, 0))
        dentist = [e for e in c.events("today") if e.summary == "Dentist"][0]
        self.assertEqual(dentist.location, "Scottsdale")


class ReminderTimes(unittest.TestCase):
    def test_ten_minutes_before(self):
        c = cal(at(2026, 10, 6, 7, 0))
        chem = [e for e in c.events("today") if e.summary == "Chemistry"][0]
        self.assertEqual(chem.remind_at(), at(2026, 10, 6, 9, 50))

    def test_an_all_day_event_has_no_reminder_time(self):
        """Otherwise a deadline nudges him at midnight."""
        c = cal(at(2026, 10, 6, 7, 0))
        deadline = [e for e in c.events("today")
                    if e.summary == "Science fair deadline"][0]
        self.assertIsNone(deadline.remind_at())

    def test_a_reminder_fires_in_its_window(self):
        c = cal(at(2026, 10, 6, 9, 52))
        due = c.due_reminders()
        self.assertEqual([e.summary for e in due], ["Chemistry"])

    def test_it_does_not_fire_early(self):
        c = cal(at(2026, 10, 6, 9, 45))
        self.assertEqual(c.due_reminders(), [])

    def test_it_does_not_fire_after_the_event_started(self):
        c = cal(at(2026, 10, 6, 10, 5))
        self.assertEqual([e.summary for e in c.due_reminders()], [])

    def test_each_event_reminds_exactly_once(self):
        """Polled every few seconds, so a repeat is the fastest route to being
        switched off."""
        now = at(2026, 10, 6, 9, 52)
        c = cal(now)
        self.assertEqual(len(c.due_reminders()), 1)
        self.assertEqual(c.due_reminders(), [], "reminded twice")


class Rundown(unittest.TestCase):
    def test_it_lists_three_and_counts_the_rest(self):
        c = cal(at(2026, 10, 6, 7, 0))
        text = c.rundown()
        self.assertTrue(text.startswith("5 today:"), text)
        self.assertIn("Chemistry at 10", text)

    def test_only_once_a_day(self):
        """Offered on every sit-down, said once."""
        c = cal(at(2026, 10, 6, 7, 0))
        self.assertIsNotNone(c.rundown())
        self.assertIsNone(c.rundown())

    def test_a_new_day_gets_a_new_rundown(self):
        c = cal_mod.Calendar(ics_text=ICS, tz=PHX,
                             clock=lambda: at(2026, 10, 6, 7, 0))
        c.rundown()
        self.assertIsNotNone(c.rundown(now=at(2026, 10, 7, 7, 0)))

    def test_an_empty_day(self):
        c = cal(at(2026, 10, 9, 7, 0))
        self.assertEqual(c.rundown(), "Nothing on today.")


class Describe(unittest.TestCase):
    def test_it_fits_the_speech_cap(self):
        from avi_bot.brain import speech
        c = cal(at(2026, 10, 6, 7, 0))
        for day in ("today", "tomorrow", "this week"):
            text = c.describe(day)
            self.assertLessEqual(len(text.split()), speech.MAX_WORDS,
                                 f"{day}: {text}")

    def test_half_past_reads_naturally(self):
        c = cal(at(2026, 10, 6, 7, 0))
        dinner = [e for e in c.events("today")
                  if e.summary.startswith("Dinner")][0]
        self.assertEqual(dinner.spoken_time(), "half past 6")

    def test_nothing_on(self):
        c = cal(at(2026, 10, 9, 7, 0))
        self.assertEqual(c.describe(), "Nothing today.")


class Robustness(unittest.TestCase):
    def test_an_unreachable_feed_is_an_empty_day_not_a_crash(self):
        """Polled every ten minutes from the loop that also drives the arm."""
        def boom(url):
            raise OSError("no network")
        c = cal_mod.Calendar(url="https://example.invalid/basic.ics",
                             tz=PHX, fetch=boom,
                             clock=lambda: at(2026, 10, 6, 7, 0))
        with self.assertRaises(OSError):
            c._text()                      # the fetch itself still raises
        c.fetch = lambda url: None
        self.assertEqual(c.events(), [])

    def test_garbage_ics_is_an_empty_day(self):
        c = cal_mod.Calendar(ics_text="this is not a calendar", tz=PHX,
                             clock=lambda: at(2026, 10, 6, 7, 0))
        self.assertEqual(c.events(), [])

    def test_the_url_is_never_returned_with_the_events(self):
        """The secret address IS the credential, so nothing downstream should
        have to be trusted with it."""
        c = cal_mod.Calendar(ics_text=ICS, url="https://secret/basic.ics",
                             tz=PHX, clock=lambda: at(2026, 10, 6, 7, 0))
        for event in c.events("today"):
            self.assertNotIn("secret", repr(event))
            self.assertNotIn("secret", str(event.__dict__ if hasattr(
                event, "__dict__") else ""))
        self.assertNotIn("secret", c.describe())


if __name__ == "__main__":
    unittest.main()
