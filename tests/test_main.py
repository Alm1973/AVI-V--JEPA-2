"""The assembly, with no hardware at all.

The point of these tests is that the whole robot can be constructed and driven
headless: no arm, no camera, no serial port, no network, no keys. If that ever
stops being true, remote development stops working.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot import main as main_mod                           # noqa: E402
from avi_bot.brain import providers, router as router_mod      # noqa: E402

ICS = (Path(__file__).resolve().parents[1] /
       "fixtures" / "ics" / "desk.ics").read_text()


class Clock:
    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s
        return self.t


def offline(name, url, model, messages, tool_list):
    raise providers.ProviderError(name, "no network in tests")


def build(**kw):
    kw.setdefault("send", offline)
    kw.setdefault("env", {})
    kw.setdefault("ics_text", ICS)
    return main_mod.Avi(**kw)


class Construction(unittest.TestCase):
    def test_it_builds_with_no_hardware(self):
        avi = build()
        self.addCleanup(avi.close)
        self.assertIsNone(avi.arm)

    def test_every_tool_has_a_handler(self):
        """A tool in the schema with nothing behind it is a promise the robot
        cannot keep, and the model has been told it can."""
        avi = build()
        self.addCleanup(avi.close)
        self.assertEqual(avi.skills.missing(), [])

    def test_a_missing_handler_is_loud(self):
        """Not a warning. The failure mode is the model calling it in a month."""
        from avi_bot.brain import tools
        # Add a tool to the SCHEMA that nothing implements. That is the real
        # failure: someone writes a schema entry, the model is told the robot
        # can do it, and nobody notices for a month. Popping a handler would
        # not test this, because _wire() simply puts it back.
        tools.BY_NAME["polish_the_brass"] = {"type": "function", "function": {
            "name": "polish_the_brass", "description": "x",
            "parameters": {"type": "object", "properties": {},
                           "required": []}}}
        try:
            with self.assertRaises(RuntimeError) as caught:
                build().close()
            self.assertIn("polish_the_brass", str(caught.exception))
        finally:
            tools.BY_NAME.pop("polish_the_brass", None)


class Turns(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.avi = build(clock=self.clock)
        self.addCleanup(self.avi.close)

    def test_a_local_command_executes_without_the_cloud(self):
        decision, results = self.avi.handle("set a timer for 5 minutes")
        self.assertEqual(decision.route, router_mod.LOCAL)
        self.assertTrue(all(r.ok for r in results), [r.error for r in results])
        self.assertEqual(len(self.avi.timers.pending()), 1)

    def test_a_skill_answer_overrides_the_model(self):
        """The skill knows; the model was guessing."""
        decision, results = self.avi.handle("what timers are there")
        self.assertIn("timer", decision.reply.say.lower())

    def test_an_action_gets_no_words(self):
        decision, results = self.avi.handle("turn the lamp off")
        self.assertIsNone(decision.reply.say)
        self.assertIsNotNone(decision.reply.beep)

    def test_the_schedule_comes_from_the_fixture(self):
        decision, results = self.avi.handle("what's on my schedule today")
        self.assertTrue(results[0].ok, results[0].error)
        self.assertIsNotNone(decision.reply.say)

    def test_an_unroutable_utterance_gives_a_confused_chirp(self):
        decision, _ = self.avi.handle("ruminate upon belt tension")
        self.assertEqual(decision.route, router_mod.CONFUSED)
        self.assertEqual(decision.reply.beep, "confused")

    def test_handle_never_raises(self):
        for text in ("", None, "x" * 4000, "???", "turn the lamp off"):
            decision, results = self.avi.handle(text)
            self.assertIsNotNone(decision.reply)

    def test_turns_accumulate_for_context(self):
        self.avi.handle("what time is it")
        self.assertGreaterEqual(len(self.avi.turns), 1)
        self.assertEqual(self.avi.turns[0]["who"], "him")

    def test_interactions_are_logged(self):
        self.avi.handle("turn the lamp off")
        rows = self.avi.store.db.execute(
            "SELECT * FROM interactions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"], router_mod.LOCAL)


class GuestModeStopsLogging(unittest.TestCase):
    def test_nothing_is_written_with_a_guest_present(self):
        """Fails closed: the camera points at someone who did not agree."""
        avi = build()
        self.addCleanup(avi.close)
        avi.handle("guest mode")
        self.assertFalse(avi.logging_enabled)
        avi.handle("what time is it")
        rows = avi.store.db.execute(
            "SELECT COUNT(*) c FROM interactions").fetchone()
        # ZERO, not one. handle() logs after the skills run, so guest mode is
        # already in force by the time its own command would be written -- the
        # utterance that turned it on is not recorded either. That is the
        # conservative reading and the right one: the moment someone asks for
        # privacy is not the moment to write one last row.
        self.assertEqual(rows["c"], 0,
                         "logged something during guest mode")


class Ticking(unittest.TestCase):
    def test_a_due_timer_surfaces_through_tick(self):
        clock = Clock()
        avi = build(clock=clock)
        self.addCleanup(avi.close)
        avi.handle("set a timer for 5 minutes")
        self.assertIsNone(avi.tick())
        clock.tick(301)
        announcement = avi.tick()
        self.assertIsNotNone(announcement)
        self.assertIn("Timer", announcement.text)

    def test_tick_is_silent_in_quiet_mode(self):
        clock = Clock()
        avi = build(clock=clock)
        self.addCleanup(avi.close)
        avi.handle("set a timer for 5 minutes")
        avi.handle("be quiet for 30 minutes")
        clock.tick(301)
        self.assertIsNone(avi.tick())


class ArmFailuresDoNotReachTheBrain(unittest.TestCase):
    class AngryArm:
        def __init__(self):
            self.config = type("C", (), {"camera": {
                "yaw_deg_per_frame": 83.9}})()

        def __getattr__(self, name):
            def boom(*a, **k):
                raise OSError("the serial port went away")
            return boom

    def test_a_failing_arm_does_not_cost_him_a_timer(self):
        avi = build(arm=self.AngryArm())
        self.addCleanup(avi.close)
        decision, results = avi.handle("set a timer for 5 minutes")
        self.assertTrue(all(r.ok for r in results))
        decision, results = avi.handle("stop")
        self.assertTrue(all(r.ok for r in results),
                        [r.error for r in results])


if __name__ == "__main__":
    unittest.main()
