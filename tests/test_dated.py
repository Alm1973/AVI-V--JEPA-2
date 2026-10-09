"""Facts that name a day, and the morning rundown that now reads them."""
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot import main as main_mod                           # noqa: E402
from avi_bot.memory import dated                               # noqa: E402

MON = datetime(2026, 10, 5, 22, 0).timestamp()        # a Monday evening


class ReadingTheDay(unittest.TestCase):
    def test_a_weekday_is_the_coming_one_from_when_it_was_said(self):
        self.assertEqual(dated.day_named("chemistry test on Friday", MON),
                         date(2026, 10, 9))

    def test_said_on_that_weekday_means_next_week(self):
        fri = datetime(2026, 10, 9, 18, 0).timestamp()
        self.assertEqual(dated.day_named("quiz on Friday", fri),
                         date(2026, 10, 16))

    def test_tomorrow_is_the_day_after_it_was_said(self):
        self.assertEqual(dated.day_named("math quiz tomorrow", MON),
                         date(2026, 10, 6))

    def test_month_and_day_either_way_round(self):
        self.assertEqual(dated.day_named("dentist on Oct 12th", MON),
                         date(2026, 10, 12))
        self.assertEqual(dated.day_named("essay due 10 October", MON),
                         date(2026, 10, 10))

    def test_a_date_already_gone_this_year_is_next_years(self):
        self.assertEqual(dated.day_named("trip on March 3", MON),
                         date(2027, 3, 3))

    def test_every_week(self):
        thu = date(2026, 10, 8)
        self.assertEqual(dated.day_named("soccer practice on Fridays", MON,
                                         today=thu), date(2026, 10, 9))

    def test_what_it_will_not_guess(self):
        for text in ("my birthday is march 3rd",       # yearly, not ahead
                     "next Friday is the dance",       # this week or next?
                     "he likes tea", "Feb 30 party"):
            with self.subTest(text):
                self.assertIsNone(dated.day_named(text, MON))

    def test_the_words_without_the_day(self):
        for text, words in (
                ("Chemistry test on Friday.", "Chemistry test"),
                ("math quiz tomorrow", "Math quiz"),
                ("chemistry test is on Friday", "Chemistry test"),
                ("Shaurya has a chemistry test on Friday", "Chemistry test"),
                ("I have an essay due tomorrow", "Essay due"),
                ("he's got soccer practice on Mondays", "Soccer practice"),
                ("dentist at 3pm on Friday", "Dentist at 3pm")):
            with self.subTest(text):
                self.assertEqual(dated.what(text), words)

    def test_ahead_keeps_today_and_tomorrow_only(self):
        facts = [{"text": "chemistry test on Friday", "created_at": MON},
                 {"text": "dentist on Oct 12th", "created_at": MON},
                 {"text": "he likes tea", "created_at": MON}]
        self.assertEqual(dated.ahead(facts, date(2026, 10, 8)),
                         [(date(2026, 10, 9), "Chemistry test")])


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def offline(name, *a):
    from avi_bot.brain import providers
    raise providers.ProviderError(name, "offline in tests")


class TheMorningRundown(unittest.TestCase):
    """Thursday 2026-10-08 08:28 he sat down; 'chemistry test on Friday'
    was in memory and the rundown said nothing."""

    def avi(self, now):
        clock = Clock(MON)
        a = main_mod.Avi(send=offline, env={}, clock=clock)
        self.addCleanup(a.close)
        a.store.remember("Chemistry test on Friday.")
        a.store.remember("he likes tea")
        clock.t = now
        return a

    def test_the_day_before(self):
        a = self.avi(datetime(2026, 10, 8, 8, 28).timestamp())
        self.assertEqual(a.rundown(), "Tomorrow: Chemistry test.")

    def test_the_day_itself(self):
        a = self.avi(datetime(2026, 10, 9, 7, 30).timestamp())
        self.assertEqual(a.rundown(), "Today: Chemistry test.")

    def test_the_day_itself_after_school_is_not_announced(self):
        a = self.avi(datetime(2026, 10, 9, 15, 30).timestamp())
        self.assertIsNone(a.rundown())

    def test_the_evening_before_still_says_tomorrow(self):
        a = self.avi(datetime(2026, 10, 8, 18, 0).timestamp())
        self.assertEqual(a.rundown(), "Tomorrow: Chemistry test.")

    def test_after_it_nothing(self):
        a = self.avi(datetime(2026, 10, 10, 9, 0).timestamp())
        self.assertIsNone(a.rundown())

    def test_with_a_reminder_today_both_are_said(self):
        now = datetime(2026, 10, 8, 8, 28).timestamp()
        a = self.avi(now)
        a.store.add_reminder(now + 3600 * 7, "call mom")
        text = a.rundown()
        self.assertTrue(text.startswith("Today: call mom at "), text)
        self.assertTrue(text.endswith("; Chemistry test tomorrow."), text)



class TheNightBefore(unittest.TestCase):
    """Morning rundown said it at 08:00; at 19:00, when he would study,
    nothing did."""

    avi = TheMorningRundown.avi

    def test_the_first_evening_sit_down_says_tomorrow_once(self):
        a = self.avi(datetime(2026, 10, 8, 19, 0).timestamp())
        self.assertEqual(a.heads_up(), "Tomorrow: Chemistry test.")
        self.assertIsNone(a.heads_up(), "said twice")

    def test_not_before_the_evening(self):
        a = self.avi(datetime(2026, 10, 8, 15, 0).timestamp())
        self.assertIsNone(a.heads_up())

    def test_an_evening_rundown_already_said_it(self):
        a = self.avi(datetime(2026, 10, 8, 19, 0).timestamp())
        self.assertEqual(a.rundown(), "Tomorrow: Chemistry test.")
        self.assertIsNone(a.heads_up())

    def test_said_once_across_a_restart(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "m.db")
        clock = Clock(MON)
        a = main_mod.Avi(db_path=path, send=offline, env={}, clock=clock)
        a.store.remember("Chemistry test on Friday.")
        clock.t = datetime(2026, 10, 8, 19, 0).timestamp()
        self.assertEqual(a.heads_up(), "Tomorrow: Chemistry test.")
        a.close()
        b = main_mod.Avi(db_path=path, send=offline, env={}, clock=clock)
        self.addCleanup(b.close)
        self.assertIsNone(b.heads_up(), "said again after a restart")

    def test_already_at_the_desk_when_the_evening_starts(self):
        from avi_bot.dashboard import brain_host as bh
        a = self.avi(datetime(2026, 10, 8, 17, 1).timestamp())
        h = bh.Host(lambda: a, present=lambda: True, note=lambda m: None)
        h.brain
        self.assertEqual(h.tick(), "Tomorrow: Chemistry test.")
        self.assertIsNone(h.tick(), "once")

    def test_reminders_due_tomorrow_too(self):
        now = datetime(2026, 10, 8, 19, 0).timestamp()
        a = self.avi(now)
        a.store.add_reminder(datetime(2026, 10, 9, 7, 0).timestamp(),
                             "bring the calculator")
        self.assertEqual(a.heads_up(), "Tomorrow: Chemistry test; bring "
                         "the calculator at 7:00 AM.")

if __name__ == "__main__":
    unittest.main()
