"""Dispatch: the bridge from a decision to an effect.

Two properties matter more than the plumbing, and both come from failures
elsewhere in this project: a call is re-validated here rather than trusted,
and a failing skill returns a refusal instead of an exception -- a tool call
that explodes must never take down the arm loop, which is exactly how a
SerialException killed the motion thread silently.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.brain import tools                               # noqa: E402
from avi_bot.memory import db as memdb                        # noqa: E402
from avi_bot.skills import memory_tools, registry, status     # noqa: E402


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.reg = registry.Registry()

    def test_a_registered_skill_runs(self):
        self.reg.register("get_time", lambda: "Quarter past nine.")
        r = self.reg.run("get_time")
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "Quarter past nine.")

    def test_args_reach_the_handler(self):
        seen = {}
        self.reg.register("lamp", lambda action: seen.setdefault("a", action))
        self.reg.run("lamp", {"action": "off"})
        self.assertEqual(seen["a"], "off")

    def test_a_call_is_revalidated_here(self):
        """The router is one caller; the dashboard and the local fallback are
        others. A guard that runs on one path is not a guard."""
        called = []
        self.reg.register("lamp", lambda action: called.append(action))
        r = self.reg.run("lamp", {"action": "disco"})
        self.assertFalse(r.ok)
        self.assertEqual(called, [], "ran an invalid call")
        self.assertIn("disco", r.error)

    def test_an_exploding_skill_is_a_refusal_not_a_crash(self):
        def boom():
            raise RuntimeError("the serial port went away")
        self.reg.register("get_time", boom)
        r = self.reg.run("get_time")
        self.assertFalse(r.ok)
        self.assertIn("serial port", r.error)

    def test_an_unwired_tool_says_so(self):
        r = self.reg.run("weather", {"day": "today"})
        self.assertFalse(r.ok)
        self.assertIn("not wired up", r.error)

    def test_an_unknown_tool_is_refused(self):
        r = self.reg.run("launch_missiles")
        self.assertFalse(r.ok)

    def test_registering_something_outside_the_schema_is_refused(self):
        """A skill the LLM was never told about can only be reached by a local
        rule, which is a silent half-feature."""
        with self.assertRaises(ValueError):
            self.reg.register("make_toast", lambda: None)

    def test_missing_lists_unkept_promises(self):
        self.reg.register("get_time", lambda: "now")
        missing = self.reg.missing()
        self.assertNotIn("get_time", missing)
        self.assertIn("weather", missing)
        self.assertEqual(set(missing) - set(tools.NAMES), set())

    def test_run_all_stops_at_the_first_failure(self):
        order = []
        self.reg.register("focus_start",
                          lambda minutes=None: order.append("focus"))
        self.reg.register("lamp", lambda action: (_ for _ in ()).throw(
            OSError("plug unreachable")))
        self.reg.register("get_time", lambda: order.append("time"))
        out = self.reg.run_all([("focus_start", {}), ("lamp", {"action": "on"}),
                                ("get_time", {})])
        self.assertEqual([r.ok for r in out], [True, False])
        self.assertEqual(order, ["focus"], "kept going after a failure")

    def test_return_shapes(self):
        self.reg.register("get_time", lambda: None)
        self.assertIsNone(self.reg.run("get_time").text)
        self.reg.handlers["get_time"] = lambda: ("text", {"k": 1})
        r = self.reg.run("get_time")
        self.assertEqual((r.text, r.data), ("text", {"k": 1}))
        self.reg.handlers["get_time"] = lambda: {"text": "dict form", "n": 2}
        self.assertEqual(self.reg.run("get_time").text, "dict form")

    def test_timing_is_recorded(self):
        self.reg.register("get_time", lambda: "now")
        self.assertGreaterEqual(self.reg.run("get_time").ms, 0.0)


class MemorySkills(unittest.TestCase):
    def setUp(self):
        self.store = memdb.Store()
        self.addCleanup(self.store.close)
        self.reg = registry.Registry()
        for name, fn in memory_tools.make(self.store).items():
            self.reg.register(name, fn)

    def test_remember_then_recall(self):
        self.assertEqual(self.reg.run("remember",
                                      {"text": "he likes mango lassi"}).text,
                         "Noted.")
        r = self.reg.run("what_do_you_know", {"query": "mango"})
        self.assertIn("mango lassi", r.text)

    def test_forget(self):
        self.reg.run("remember", {"text": "a thing"})
        self.assertEqual(self.reg.run("forget", {"text": "a thing"}).text,
                         "Forgotten.")
        self.assertEqual(self.reg.run("forget", {"text": "a thing"}).text,
                         "I didn't have that.")

    def test_a_too_long_fact_is_reported_not_raised(self):
        r = self.reg.run("remember", {"text": "x" * 279})
        self.assertTrue(r.ok)
        # 281 exceeds the schema's maxLength, so validation catches it first.
        r = self.reg.run("remember", {"text": "x" * 281})
        self.assertFalse(r.ok)

    def test_an_eviction_is_announced(self):
        """Silently forgetting something to make room makes a memory feel
        untrustworthy."""
        for i in range(memdb.MAX_FACTS):
            self.store.remember(f"filler fact {i}")
        r = self.reg.run("remember", {"text": "the one that forces a drop"})
        self.assertIn("Forgot", r.text)

    def test_nothing_known_says_so(self):
        self.assertEqual(self.reg.run("what_do_you_know").text, "Nothing yet.")
        self.assertIn("Nothing about that",
                      self.reg.run("what_do_you_know",
                                   {"query": "quantum"}).text)

    def test_answers_fit_the_speech_cap(self):
        from avi_bot.brain import speech
        for i in range(4):
            self.store.remember(f"a fact about school number {i}")
        text = self.reg.run("what_do_you_know", {"query": "school"}).text
        self.assertLessEqual(len(text.split()), speech.MAX_WORDS)


class Status(unittest.TestCase):
    class Mood:
        def emotion(self):
            return "curious"

    class Clock:
        def __init__(self):
            self.t = 1000.0

        def __call__(self):
            return self.t

    def test_it_works_when_everything_is_missing(self):
        """The moment this is most needed is the moment something is broken."""
        handlers = status.make(mood=None, budget=None, vram=lambda: None)
        text, data = handlers["robot_status"]()
        self.assertIn("Up ", text)
        self.assertIsNone(data["mood"])

    def test_a_broken_mood_source_does_not_break_status(self):
        class Angry:
            def emotion(self):
                raise RuntimeError("mood module is the thing that is broken")
        handlers = status.make(mood=Angry(), vram=lambda: None)
        text, data = handlers["robot_status"]()
        self.assertIsNone(data["mood"])
        self.assertIn("Up ", text)

    def test_it_reports_what_it_has(self):
        clock = self.Clock()
        budget = type("B", (), {"report": lambda self: {
            "groq": {"requests": 7, "failures": 1}}})()
        handlers = status.make(mood=self.Mood(), budget=budget,
                               started_at=clock() - 7260, vram=lambda: {
                                   "free_GB": 5.3, "total_GB": 6.44},
                               clock=clock)
        text, data = handlers["robot_status"]()
        self.assertIn("curious", text)
        self.assertIn("5.3GB free", text)
        self.assertIn("7 calls", text)
        self.assertIn("1 failed", text)
        self.assertTrue(text.startswith("Up 2h01"), text)

    def test_uptime_units(self):
        clock = self.Clock()
        for ago, want in ((30, "30s"), (600, "10m"), (7200, "2h00")):
            handlers = status.make(started_at=clock() - ago, vram=lambda: None,
                                   clock=clock)
            self.assertIn(want, handlers["robot_status"]()[0])


if __name__ == "__main__":
    unittest.main()
