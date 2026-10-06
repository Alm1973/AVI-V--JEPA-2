"""BUILD_SPEC 9's test: "10 phrasings parse correctly".

Twenty are checked, from a fixed instant so the forward-resolution rule can be
tested from both sides of the clock. The second class matters as much: phrases
that must return None rather than a guess, because a wrong reminder time is
worse than a refused one -- he stops checking.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.memory import db as memdb                        # noqa: E402
from avi_bot.skills import registry, reminders                # noqa: E402

# A Tuesday morning, 09:00.
NOW = dt.datetime(2026, 10, 6, 9, 0)


def p(phrase, now=NOW):
    return reminders.parse(phrase, now)


class TwentyPhrasings(unittest.TestCase):
    CASES = [
        ("in 20 min",            dt.datetime(2026, 10, 6, 9, 20)),
        ("in 20 minutes",        dt.datetime(2026, 10, 6, 9, 20)),
        ("in 5m",                dt.datetime(2026, 10, 6, 9, 5)),
        ("in two hours",         dt.datetime(2026, 10, 6, 11, 0)),
        ("in an hour",           dt.datetime(2026, 10, 6, 10, 0)),
        ("in half an hour",      dt.datetime(2026, 10, 6, 9, 30)),
        ("in 30 seconds",        dt.datetime(2026, 10, 6, 9, 0, 30)),
        ("in 2 days",            dt.datetime(2026, 10, 8, 9, 0)),
        ("at 5",                 dt.datetime(2026, 10, 6, 17, 0)),
        ("at 5pm",               dt.datetime(2026, 10, 6, 17, 0)),
        ("at 10:30",             dt.datetime(2026, 10, 6, 10, 30)),
        ("at 10:30am",           dt.datetime(2026, 10, 6, 10, 30)),
        ("at 7:45 p.m.",         dt.datetime(2026, 10, 6, 19, 45)),
        ("noon",                 dt.datetime(2026, 10, 6, 12, 0)),
        ("at midnight",          dt.datetime(2026, 10, 7, 0, 0)),
        ("this evening",         dt.datetime(2026, 10, 6, 19, 0)),
        ("tonight",              dt.datetime(2026, 10, 6, 21, 0)),
        ("after dinner",         None),           # must refuse, see below
        ("tomorrow at 8",        dt.datetime(2026, 10, 7, 8, 0)),
        ("tomorrow morning",     dt.datetime(2026, 10, 7, 8, 0)),
    ]

    def test_at_least_ten_parse(self):
        ok, wrong = 0, []
        for phrase, want in self.CASES:
            got = p(phrase)
            if got == want:
                ok += 1
            else:
                wrong.append(f"{phrase!r}: {got} not {want}")
        self.assertGreaterEqual(ok, 10,
                                f"{ok}/{len(self.CASES)}:\n  "
                                + "\n  ".join(wrong))

    def test_how_many_actually_parse(self):
        ok = sum(1 for phrase, want in self.CASES if p(phrase) == want)
        print(f"\n  reminder phrasings: {ok}/{len(self.CASES)}")
        self.assertEqual(ok, len(self.CASES), "a phrasing regressed")


class ForwardResolution(unittest.TestCase):
    """Nobody sets a reminder for the past."""

    def test_at_five_in_the_morning_means_this_afternoon(self):
        self.assertEqual(p("at 5", dt.datetime(2026, 10, 6, 9, 0)),
                         dt.datetime(2026, 10, 6, 17, 0))

    def test_at_five_in_the_evening_means_tomorrow_morning(self):
        self.assertEqual(p("at 5", dt.datetime(2026, 10, 6, 18, 0)),
                         dt.datetime(2026, 10, 7, 5, 0))

    def test_an_explicit_am_is_respected_not_rolled_forward(self):
        self.assertEqual(p("at 5am", dt.datetime(2026, 10, 6, 9, 0)),
                         dt.datetime(2026, 10, 7, 5, 0))

    def test_noon_before_noon(self):
        self.assertEqual(p("noon", dt.datetime(2026, 10, 6, 11, 0)),
                         dt.datetime(2026, 10, 6, 12, 0))

    def test_noon_after_noon_rolls_to_tomorrow(self):
        self.assertEqual(p("noon", dt.datetime(2026, 10, 6, 13, 0)),
                         dt.datetime(2026, 10, 7, 12, 0))

    def test_a_parsed_time_is_never_in_the_past(self):
        for hour in range(24):
            now = dt.datetime(2026, 10, 6, hour, 17)
            for phrase in ("at 5", "noon", "tonight", "at 8:30", "in 10 min"):
                got = p(phrase, now)
                if got is not None:
                    self.assertGreater(got, now, f"{phrase!r} at {hour}:17")


class RefusesRatherThanGuessing(unittest.TestCase):
    """A wrong reminder time is worse than a refused one."""

    JUNK = ["after dinner", "later", "soon", "when I'm done",
            "at some point", "in a bit", "next week sometime",
            "", "   ", None, "at 99", "at 10:99", "in 0 minutes",
            "in minutes", "at", "banana o'clock", "in -5 minutes"]

    def test_all_refused(self):
        claimed = [(j, p(j)) for j in self.JUNK]
        claimed = [(j, got) for j, got in claimed if got is not None]
        self.assertEqual(claimed, [], f"guessed at: {claimed}")

    def test_an_impossible_clock_time_is_refused(self):
        self.assertIsNone(p("at 25:00"))
        self.assertIsNone(p("at 13pm"))


class TheSkill(unittest.TestCase):
    def setUp(self):
        self.store = memdb.Store()
        self.addCleanup(self.store.close)
        self.reg = registry.Registry()
        for name, fn in reminders.make(self.store,
                                       clock=lambda: NOW).items():
            self.reg.register(name, fn)

    def test_a_reminder_is_stored_at_the_right_time(self):
        r = self.reg.run("set_reminder",
                         {"when": "in 20 min", "text": "stand up"})
        self.assertTrue(r.ok)
        rows = self.store.due_reminders(
            now=(NOW + dt.timedelta(minutes=21)).timestamp())
        self.assertEqual([x["text"] for x in rows], ["stand up"])

    def test_it_is_not_due_early(self):
        self.reg.run("set_reminder", {"when": "at 5", "text": "call home"})
        self.assertEqual(self.store.due_reminders(now=NOW.timestamp()), [])

    def test_an_unparseable_time_asks_rather_than_guessing(self):
        r = self.reg.run("set_reminder",
                         {"when": "after dinner", "text": "homework"})
        self.assertTrue(r.ok)
        self.assertIn("When is", r.text)
        self.assertIsNone(r.data["parsed"])
        self.assertEqual(self.store.due_reminders(
            now=(NOW + dt.timedelta(days=2)).timestamp()), [],
            "stored a reminder it could not place in time")

    def test_the_confirmation_fits_the_speech_cap(self):
        from avi_bot.brain import speech
        for when in ("in 20 min", "at 5", "tomorrow at 8", "after dinner"):
            r = self.reg.run("set_reminder", {"when": when, "text": "x"})
            self.assertLessEqual(len(r.text.split()), speech.MAX_WORDS, r.text)


if __name__ == "__main__":
    unittest.main()
