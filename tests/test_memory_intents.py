"""Questions about what AVI knows are answered locally, never guessed."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.brain import intents                                  # noqa: E402


def m(text):
    got = intents.match(text)
    return (got.tool, got.args) if got else None


class Memory(unittest.TestCase):
    def test_what_do_you_know_about_me(self):
        """The cloud model answered this WITHOUT the tool: "I have no stored
        facts", with a fact stored."""
        self.assertEqual(m("what do you know about me"),
                         ("what_do_you_know", {}))
        self.assertEqual(m("Hey AVI, what have you noticed?"),
                         ("what_do_you_know", {}))

    def test_about_a_thing(self):
        self.assertEqual(m("what have you noticed about my desk"),
                         ("what_do_you_know", {"query": "desk"}))

    def test_remember_and_forget(self):
        self.assertEqual(m("remember that my chemistry test is on friday"),
                         ("remember", {"text": "my chemistry test is on "
                                               "friday"}))
        self.assertEqual(m("forget about the chemistry test"),
                         ("forget", {"text": "the chemistry test"}))

    def test_remember_to_is_a_reminder_not_a_fact(self):
        got = intents.match("remember to take the trash out at 5")
        self.assertTrue(got is None or got.tool != "remember")

    def test_which_timers(self):
        self.assertEqual(m("what timers do I have"), ("list_timers", {}))

    def test_finding_still_finds(self):
        self.assertEqual(m("where's my pen"), ("find_object", {"name": "pen"}))


if __name__ == "__main__":
    unittest.main()
