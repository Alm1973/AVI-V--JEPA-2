"""Spoken numbers. Small module, load-bearing: every spoken command with a
duration goes through it."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot import numbers                                   # noqa: E402


class WordsToInt(unittest.TestCase):
    def test_singles(self):
        for word, want in (("five", 5), ("ten", 10), ("fifteen", 15),
                           ("nineteen", 19), ("twenty", 20), ("ninety", 90)):
            self.assertEqual(numbers.words_to_int(word), want, word)

    def test_compounds(self):
        for word, want in (("twenty five", 25), ("thirty five", 35),
                           ("forty five", 45), ("ninety nine", 99),
                           ("twenty-five", 25), ("Twenty  Five", 25)):
            self.assertEqual(numbers.words_to_int(word), want, word)

    def test_digits_pass_through(self):
        self.assertEqual(numbers.words_to_int("25"), 25)
        self.assertEqual(numbers.words_to_int("0"), 0)

    def test_quantifiers_only_when_asked(self):
        self.assertIsNone(numbers.words_to_int("an"))
        self.assertEqual(numbers.words_to_int("an", allow_quantifiers=True), 1)
        self.assertEqual(numbers.words_to_int("a couple",
                                              allow_quantifiers=True), None)

    def test_a_misspelling_transcripts_produce(self):
        self.assertEqual(numbers.words_to_int("fourty five"), 45)

    def test_it_refuses_rather_than_guessing(self):
        """'five twenty' is a time and 'twenty twenty' is a year. Both would
        be wrong answers dressed up as right ones."""
        for junk in ("five twenty", "twenty twenty", "twenty hundred",
                     "banana", "", "   ", None, "five five",
                     "one two three", "minus five"):
            self.assertIsNone(numbers.words_to_int(junk), repr(junk))

    def test_zero_is_a_number_not_a_falsy_refusal(self):
        self.assertEqual(numbers.words_to_int("zero"), 0)
        self.assertIsNotNone(numbers.words_to_int("zero"))


class ThroughTheRouter(unittest.TestCase):
    """The bug this module was written for: a transcript spells numbers out,
    so the typed path worked and the spoken path silently lost the duration."""

    def test_spoken_durations_route_with_their_number(self):
        from avi_bot.brain import intents
        cases = {
            "focus for twenty five minutes": ("focus_start", {"minutes": 25}),
            "focus for thirty minutes": ("focus_start", {"minutes": 30}),
            "timer for twenty minutes": ("set_timer", {"seconds": 1200}),
            "timer for two hours": ("set_timer", {"seconds": 7200}),
            "set a timer for ninety minutes": ("set_timer", {"seconds": 5400}),
            "focus for an hour": ("focus_start", {"minutes": 60}),
        }
        for text, (tool, args) in cases.items():
            intent = intents.match(text)
            self.assertIsNotNone(intent, text)
            self.assertEqual((intent.tool, intent.args), (tool, args), text)

    def test_spoken_durations_parse_as_reminder_times(self):
        import datetime as dt
        from avi_bot.skills import reminders
        now = dt.datetime(2026, 10, 6, 9, 0)
        self.assertEqual(reminders.parse("in twenty five minutes", now),
                         dt.datetime(2026, 10, 6, 9, 25))
        self.assertEqual(reminders.parse("in ninety minutes", now),
                         dt.datetime(2026, 10, 6, 10, 30))


if __name__ == "__main__":
    unittest.main()
