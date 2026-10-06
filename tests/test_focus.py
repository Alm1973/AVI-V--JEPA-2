"""Focus mode, and the amended trigger.

BUILD_SPEC 9's test is "replay fixture frames -> the right nudges at the right
times". Frames are the wrong input to test against: what the skill actually
consumes is a sequence of phone verdicts and breakpoints, so that is what gets
replayed. Testing it through frames would be testing YOLO.

The headline property is the one the research forced: phone time alone must
NEVER produce a nudge.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.skills import focus as focus_mod                 # noqa: E402


class Clock:
    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s
        return self.t


def session(minutes=None):
    clock = Clock()
    f = focus_mod.Focus(clock=clock)
    f.start(minutes)
    return f, clock


def distract(f, clock, seconds, step=10.0):
    """Hold the phone for `seconds`, in realistic small observations."""
    done = 0.0
    f.saw_phone(True)
    while done < seconds:
        clock.tick(step)
        f.saw_phone(True)
        done += step


class TheTriggerIsNotAStopwatch(unittest.TestCase):
    def test_phone_time_alone_never_nudges(self):
        """The whole point. Elapsed time is a poor indicator of readiness, and
        a mistimed nudge trains him to ignore AVI from then on."""
        f, clock = session()
        distract(f, clock, 600)                 # ten minutes on the phone
        for _ in range(50):
            clock.tick(10)
            self.assertIsNone(f.tick(breakpoint=False))

    def test_a_breakpoint_alone_never_nudges(self):
        """No evidence, no nudge -- otherwise it fires at every settle."""
        f, clock = session()
        for _ in range(20):
            clock.tick(30)
            self.assertIsNone(f.tick(breakpoint=True))

    def test_evidence_plus_a_breakpoint_nudges(self):
        f, clock = session()
        distract(f, clock, 180)
        self.assertEqual(f.tick(breakpoint=True), focus_mod.AHEM)

    def test_the_first_nudge_is_wordless(self):
        f, clock = session()
        distract(f, clock, 180)
        self.assertEqual(f.tick(breakpoint=True), focus_mod.AHEM)

    def test_the_second_nudge_gets_words(self):
        f, clock = session()
        distract(f, clock, 180)
        f.tick(breakpoint=True)
        clock.tick(focus_mod.NUDGE_GAP_S + 1)
        distract(f, clock, 180)
        self.assertEqual(f.tick(breakpoint=True), focus_mod.WORDS)

    def test_at_most_two_nudges_a_session(self):
        f, clock = session()
        for _ in range(5):
            distract(f, clock, 180)
            f.tick(breakpoint=True)
            clock.tick(focus_mod.NUDGE_GAP_S + 1)
        self.assertLessEqual(f.session.nudges, focus_mod.MAX_NUDGES)

    def test_a_spent_nudge_does_not_refire_on_the_next_breakpoint(self):
        """Carrying the evidence forward would nudge twice for one stretch."""
        f, clock = session()
        distract(f, clock, 300)
        self.assertEqual(f.tick(breakpoint=True), focus_mod.AHEM)
        clock.tick(focus_mod.NUDGE_GAP_S + 1)
        self.assertIsNone(f.tick(breakpoint=True))

    def test_two_nudges_are_never_back_to_back(self):
        f, clock = session()
        distract(f, clock, 180)
        f.tick(breakpoint=True)
        distract(f, clock, 180)
        clock.tick(10)
        self.assertIsNone(f.tick(breakpoint=True), "nudged twice in a minute")


class PhoneAccounting(unittest.TestCase):
    def test_putting_the_phone_down_stops_the_clock(self):
        f, clock = session()
        distract(f, clock, 60)
        f.saw_phone(False)
        clock.tick(600)
        f.saw_phone(False)
        self.assertLess(f.session.phone_s, focus_mod.PHONE_EVIDENCE_S)
        self.assertIsNone(f.tick(breakpoint=True))

    def test_interrupted_phone_use_still_accumulates(self):
        f, clock = session()
        for _ in range(4):
            distract(f, clock, 40)
            f.saw_phone(False)
            clock.tick(30)
        self.assertGreaterEqual(f.session.phone_s, focus_mod.PHONE_EVIDENCE_S)

    def test_observations_outside_a_session_are_ignored(self):
        clock = Clock()
        f = focus_mod.Focus(clock=clock)
        f.saw_phone(True)
        clock.tick(300)
        f.saw_phone(True)
        self.assertIsNone(f.tick(breakpoint=True))


class Breaks(unittest.TestCase):
    def test_a_break_is_suggested_after_fifty_minutes(self):
        f, clock = session()
        clock.tick(focus_mod.BREAK_AFTER_S + 1)
        self.assertEqual(f.tick(breakpoint=True), focus_mod.BREAK)

    def test_a_break_still_waits_for_a_breakpoint(self):
        """Interrupting someone mid-thought to suggest a rest is the exact
        failure this module is built around."""
        f, clock = session()
        clock.tick(focus_mod.BREAK_AFTER_S + 1)
        self.assertIsNone(f.tick(breakpoint=False))
        self.assertEqual(f.tick(breakpoint=True), focus_mod.BREAK)

    def test_a_break_is_offered_once(self):
        f, clock = session()
        clock.tick(focus_mod.BREAK_AFTER_S + 1)
        self.assertEqual(f.tick(breakpoint=True), focus_mod.BREAK)
        clock.tick(focus_mod.NUDGE_GAP_S + 1)
        self.assertIsNone(f.tick(breakpoint=True))


class Lifecycle(unittest.TestCase):
    def test_status_counts_down_when_a_length_was_given(self):
        f, clock = session(minutes=25)
        clock.tick(600)
        text, data = f.status()
        self.assertEqual(data["left"], 15)

    def test_status_counts_up_otherwise(self):
        f, clock = session()
        clock.tick(600)
        text, data = f.status()
        self.assertEqual(data["elapsed"], 10)

    def test_starting_twice_does_not_reset_the_session(self):
        f, clock = session(minutes=25)
        clock.tick(600)
        f.start(25)
        self.assertEqual(f.status()[1]["elapsed"], 10)

    def test_the_summary_reports_phone_time(self):
        f, clock = session()
        distract(f, clock, 300)
        clock.tick(1200)
        text, data = f.stop()
        self.assertGreaterEqual(data["phone_minutes"], 5)
        self.assertIn("on your phone", text)

    def test_a_clean_session_is_praised(self):
        f, clock = session()
        clock.tick(1500)
        text, data = f.stop()
        self.assertEqual(data["phone_minutes"], 0)
        self.assertIn("no phone", text)

    def test_stopping_when_not_focusing(self):
        clock = Clock()
        f = focus_mod.Focus(clock=clock)
        self.assertEqual(f.stop()[0], "Not focusing.")
        self.assertEqual(f.status()[0], "Not focusing.")

    def test_every_sentence_fits_the_speech_cap(self):
        from avi_bot.brain import speech
        f, clock = session(minutes=25)
        texts = [f.status()[0]]
        distract(f, clock, 300)
        clock.tick(1200)
        texts.append(f.stop()[0])
        f2, c2 = session()
        c2.tick(1500)
        texts.append(f2.stop()[0])
        for text in texts:
            self.assertLessEqual(len(text.split()), speech.MAX_WORDS, text)

    def test_the_handlers_wire_into_the_registry(self):
        from avi_bot.skills import registry
        clock = Clock()
        f = focus_mod.Focus(clock=clock)
        reg = registry.Registry()
        for name, fn in focus_mod.make(f).items():
            reg.register(name, fn)
        self.assertTrue(reg.run("focus_start", {"minutes": 25}).ok)
        self.assertTrue(f.active)
        self.assertTrue(reg.run("focus_status").ok)
        self.assertTrue(reg.run("focus_stop").ok)
        self.assertFalse(f.active)


if __name__ == "__main__":
    unittest.main()
