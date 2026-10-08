"""The local router, and M4's acceptance test.

BUILD_SPEC 15 M4: "at least 18 of 20 scripted utterances give the right tool
calls". The twenty below are the frequent commands from 8.1, phrased the way
someone actually speaks rather than the way a regex wants to be fed.

The second class matters as much: things the local layer must NOT claim. A
wrong local match is instant, confident, and never reaches a model that would
have read the whole sentence, so falling through is the correct behaviour for
anything ambiguous.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.brain import intents                            # noqa: E402

SCRIPTED = [
    ("set a timer for 5 minutes",        "set_timer", {"seconds": 300}),
    ("timer for 20 min",                 "set_timer", {"seconds": 1200}),
    ("10 minute timer",                  "set_timer", {"seconds": 600}),
    ("hey avi, set a timer for 1 hour",  "set_timer", {"seconds": 3600}),
    ("cancel the timer",                 "cancel_timer", {}),
    ("what timers are there",            "list_timers", {}),
    ("focus for 25 minutes",             "focus_start", {"minutes": 25}),
    ("start focus mode",                 "focus_start", {}),
    ("stop focus",                       "focus_stop", {}),
    ("turn the lamp on",                 "lamp", {"action": "on"}),
    ("turn off the lights",              "lamp", {"action": "off"}),
    ("toggle the lamp",                  "lamp", {"action": "toggle"}),
    ("next song",                        "media", {"action": "next"}),
    ("volume up",                        "media", {"action": "volume_up"}),
    ("where's my phone?",                "find_object", {"name": "phone"}),
    ("have you seen my glasses",         "find_object", {"name": "glasses"}),
    ("what time is it",                  "get_time", {}),
    ("good job",                         "praise", {}),
    ("go to sleep",                      "sleep", {}),
    ("what's on my schedule today",      "get_schedule", {"day": "today"}),
]

# Must fall through to the cloud: open-ended, or a command the local layer has
# no business guessing at.
MUST_FALL_THROUGH = [
    "why is the elbow servo buzzing when you lean forward",
    "remind me to email my teacher about the science fair after dinner",
    "how was my focus this week compared to last week",
    "what do you think of the new arm bracket",
    "tell me something interesting about jellyfish",
    "move your arm a bit to the left and then look at the window",
]


class Scripted(unittest.TestCase):
    def test_at_least_18_of_20(self):
        got, wrong = 0, []
        for text, tool, args in SCRIPTED:
            intent = intents.match(text)
            if intent is None:
                wrong.append(f"{text!r}: no match")
            elif intent.tool != tool:
                wrong.append(f"{text!r}: {intent.tool} not {tool}")
            elif intent.args != args:
                wrong.append(f"{text!r}: args {intent.args} not {args}")
            else:
                got += 1
        self.assertGreaterEqual(got, 18,
                                f"{got}/20. Misses:\n  " + "\n  ".join(wrong))

    def test_how_far_is_a_find(self):
        """The model made up "0.35 m" for the pen; the rule's hint sends
        it to find_object, which reads the desk scan."""
        for text, what in (("how far away is my pen", "pen"),
                           ("how far is the book from you?", "book"),
                           ("how far is my mug away", "mug")):
            with self.subTest(text):
                i = intents.match(text)
                self.assertEqual((i.tool, i.args), ("find_object",
                                                    {"name": what}))

    def test_all_twenty_would_be_better(self):
        """Not the acceptance bar, but record where it actually stands."""
        got = sum(1 for t, tool, a in SCRIPTED
                  if (i := intents.match(t)) and i.tool == tool and i.args == a)
        print(f"\n  local router: {got}/20 exact")
        self.assertGreaterEqual(got, 18)


class FallsThrough(unittest.TestCase):
    def test_open_ended_is_not_claimed(self):
        claimed = [(t, intents.match(t)) for t in MUST_FALL_THROUGH]
        claimed = [(t, i) for t, i in claimed if i is not None]
        self.assertEqual(claimed, [],
                         "local rules claimed an open-ended utterance; a wrong "
                         "instant match never reaches the model")

    def test_empty_and_noise(self):
        for junk in ("", "   ", None, "hey avi", "uh", "....", "?"):
            self.assertIsNone(intents.match(junk), repr(junk))


class StopIsNotMedia(unittest.TestCase):
    def test_bare_stop_is_the_emergency_stop(self):
        """BUILD_SPEC 8.1 lists `stop` among the modes. "stop" alone must halt
        the robot, not pause music -- the two have very different costs when
        guessed wrong."""
        self.assertEqual(intents.match("stop").tool, "stop")

    def test_stop_the_music_is_media(self):
        self.assertEqual(intents.match("stop the music").tool, "media")


class Durations(unittest.TestCase):
    def test_words_and_digits(self):
        self.assertEqual(intents._seconds("5 minutes"), 300)
        self.assertEqual(intents._seconds("twenty minutes"), 1200)
        self.assertEqual(intents._seconds("1 hour"), 3600)
        self.assertEqual(intents._seconds("30 seconds"), 30)
        self.assertEqual(intents._minutes("half an hour"), 30)

    def test_sub_minute_does_not_round_to_zero(self):
        """"5 second timer" is a real thing people say."""
        self.assertEqual(intents._minutes("5 seconds"), 1)
        self.assertEqual(intents._seconds("5 seconds"), 5)

    def test_nonsense_duration_is_none(self):
        self.assertIsNone(intents._seconds("banana"))
        self.assertIsNone(intents._seconds(""))


if __name__ == "__main__":
    unittest.main()
