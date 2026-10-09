"""Shortcuts AVI learns from its own turns (brain/shortcuts.py): the same
words answered the same way twice stop needing the cloud."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.brain import providers, router as router_mod      # noqa: E402
from avi_bot.brain import shortcuts as sc                      # noqa: E402
from avi_bot.memory import db as memdb                         # noqa: E402
from tests.test_brain import FakeCall, FakeResult, chain       # noqa: E402
from tests.test_main import Clock, build                       # noqa: E402


class Model:
    """A cloud that answers with `calls`, counting how often it is asked;
    `down` makes it unreachable."""

    def __init__(self, calls):
        self.calls, self.asked, self.down = calls, 0, False

    def __call__(self, name, url, model, messages, tool_list):
        if self.down:
            raise providers.ProviderError(name, "down")
        self.asked += 1
        return FakeResult(content='{"beep": "neutral"}',
                          calls=[FakeCall(n, a) for n, a in self.calls])


class Learning(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = memdb.Store(clock=self.clock)
        self.addCleanup(self.store.close)
        self.short = sc.Shortcuts(self.store)
        self.model = Model([("perform", '{"name": "dolphin", "moves": '
                                        '[{"move": "leap"}]}')])
        self.router = router_mod.Router(chain=chain(self.model),
                                        cloud_first=True, clock=self.clock,
                                        shortcuts=self.short)

    def ask(self, text):
        return self.router.handle(text)

    def test_twice_the_same_and_the_third_time_is_instant(self):
        for _ in range(2):
            self.assertEqual(self.ask("Hey AVI, swim like a dolphin!").route,
                             router_mod.CLOUD)
        d = self.ask("swim like a dolphin")
        self.assertEqual(d.route, router_mod.LEARNED)
        self.assertEqual(d.calls, [("perform", {"name": "dolphin"})])
        self.assertEqual(self.model.asked, 2)

    def test_different_answers_start_over(self):
        self.ask("swim like a dolphin")
        self.model.calls = [("play_emotion", '{"name": "wave"}')]
        self.ask("swim like a dolphin")
        self.assertEqual(self.ask("swim like a dolphin").route,
                         router_mod.CLOUD)

    def test_seen_once_is_a_fallback_only(self):
        self.ask("swim like a dolphin")
        self.model.down = True
        d = self.ask("swim like a dolphin")
        self.assertEqual(d.route, router_mod.LEARNED)

    def test_praise_proves_it_a_scold_drops_it(self):
        self.ask("swim like a dolphin")
        self.assertEqual(self.short.feedback(True), "swim like a dolphin")
        self.assertEqual(self.ask("swim like a dolphin").route,
                         router_mod.LEARNED)
        self.assertEqual(self.short.feedback(False), "swim like a dolphin")
        self.assertIsNone(self.short.lookup("swim like a dolphin"))

    def test_late_feedback_is_not_about_it(self):
        self.ask("swim like a dolphin")
        self.clock.tick(600)
        self.assertIsNone(self.short.feedback(True))

    def test_words_that_point_back_are_never_learned(self):
        for text in ("do that again", "turn it off", "yes"):
            self.assertFalse(sc.learnable(text, [("stop", {})]), text)
        self.assertFalse(sc.learnable("remember I like tea",
                                      [("remember", {"text": "tea"})]))

    def test_stop_is_never_delayed_by_a_shortcut(self):
        self.store.db.execute(
            "INSERT INTO shortcuts (key, calls, seen, proven, created_at)"
            " VALUES ('stop', '[[\"play_emotion\", {\"name\": \"wave\"}]]',"
            " 5, 1, 0)")
        self.assertEqual(self.ask("stop").calls, [("stop", {})])


class InTheRobot(unittest.TestCase):
    def test_good_job_proves_the_turn_before_it(self):
        model = Model([("play_emotion", '{"name": "wave"}')])
        avi = build(clock=Clock(), send=model, cloud_first=True,
                    env={"GROQ_API_KEY": "x"})
        self.addCleanup(avi.close)
        avi.handle("give me a big hello")
        model.calls = [("praise", "{}")]
        avi.handle("good job avi")
        got = avi.router.shortcuts.lookup("give me a big hello")
        self.assertEqual(got, ([("play_emotion", {"name": "wave"})], True))


if __name__ == "__main__":
    unittest.main()
