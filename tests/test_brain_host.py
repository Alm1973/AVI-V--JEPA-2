"""The voice brain hosted with the real arm: adapter and delivery rules."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.dashboard import brain_host as bh                     # noqa: E402


class Engine:
    def __init__(self):
        self.cleared = 0

    def clear(self):
        self.cleared += 1


class FakeArm:
    def __init__(self):
        self.engine = Engine()
        self.config = "cfg"


class Adapter(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.body = bh.BrainArm(
            FakeArm(), recall=lambda n: self.calls.append(("recall", n)),
            find=lambda n, look: self.calls.append(("find", n, look)) or
            {"say": f"found {n}"})

    def test_looking_at_him_goes_to_the_seat_view(self):
        self.body.look_at("me")
        self.assertEqual(self.calls, [("recall", "ATTENTIVE")])

    def test_looking_at_a_thing_finds_it(self):
        self.body.look_at("mug")
        self.assertEqual(self.calls, [("find", "mug", True)])

    def test_cancel_clears_the_queue(self):
        self.body.cancel()
        self.assertEqual(self.body._arm.engine.cleared, 1)

    def test_everything_else_passes_through(self):
        self.assertEqual(self.body.config, "cfg")

    def test_the_brain_finds_through_the_dashboard(self):
        """main.Avi._find uses the host's find -- the one with the world map,
        the backlash-safe turn and the desk layout."""
        from avi_bot import main as main_mod
        body = bh.BrainArm(FakeArm(), recall=lambda n: None,
                           find=lambda n, look: {
                               "say": "The desk, around 4:58. Left of the "
                                      "laptop.",
                               "moved": True,
                               "aim": {"source": "map", "confident": True}})
        avi = main_mod.Avi(arm=body, send=lambda *a: None, env={})
        self.addCleanup(avi.close)
        text, info = avi._find("pen")
        self.assertIn("Left of the laptop", text)
        self.assertTrue(info["moved"])


class Ann:
    def __init__(self, text="Timer done."):
        self.kind, self.text = "timer", text


class Sched:
    def __init__(self):
        self.done = []

    def delivered_ok(self, a):
        self.done.append(a)


class Brain:
    def __init__(self, pending):
        self.pending = list(pending)
        self.scheduler = Sched()

    def tick(self):
        return self.pending[0] if self.pending else None


class Delivery(unittest.TestCase):
    def host(self, here, pending=(Ann(),)):
        cues = []
        h = bh.Host(lambda: Brain(pending), present=lambda: here,
                    note=lambda m: None, cue=lambda: cues.append(1))
        h.brain                                    # build it
        return h, cues

    def test_delivered_when_he_is_there(self):
        h, cues = self.host(here=True)
        self.assertEqual(h.tick(), "Timer done.")
        self.assertEqual(len(cues), 1)
        self.assertEqual(len(h.brain.scheduler.done), 1)

    def test_an_empty_room_is_not_a_delivery(self):
        h, cues = self.host(here=False)
        self.assertIsNone(h.tick())
        self.assertEqual(cues, [])
        self.assertEqual(h.brain.scheduler.done, [], "retired undelivered")

    def test_the_loop_never_builds_the_brain(self):
        built = []
        h = bh.Host(lambda: built.append(1) or Brain([]), note=lambda m: None)
        h.tick()
        self.assertEqual(built, [])

    def test_a_brain_that_will_not_start_is_reported_not_raised(self):
        def boom():
            raise RuntimeError("no keys")
        h = bh.Host(boom, note=lambda m: None)
        self.assertIn("no keys", h.ask("hi")["error"])


if __name__ == "__main__":
    unittest.main()


class Threads(unittest.TestCase):
    def test_the_real_brain_answers_from_another_thread(self):
        """Every /ask after the first failed: 'SQLite objects created in a
        thread can only be used in that same thread'."""
        import threading
        from avi_bot import main as main_mod
        h = bh.Host(lambda: main_mod.Avi(send=lambda *a: None, env={}),
                    note=lambda m: None)
        first = h.ask("what time is it")
        self.assertNotIn("error", first)
        got = {}
        t = threading.Thread(target=lambda: got.update(
            h.ask("set a timer for 2 minutes")))
        t.start()
        t.join()
        self.assertNotIn("error", got, got)
        h.brain.close()


class RepliesAreMoreThanWords(unittest.TestCase):
    def test_a_confused_turn_shakes(self):
        from avi_bot import main as main_mod
        from avi_bot.brain import providers

        def offline(*a):
            raise providers.ProviderError("all", "offline")
        done = []
        h = bh.Host(lambda: main_mod.Avi(send=offline, env={}),
                    note=lambda m: None, perform=done.append)
        out = h.ask("xyzzy plugh frobnicate")
        self.assertEqual(out["gesture"], "shake")
        self.assertEqual(done, ["shake"])
        h.brain.close()
