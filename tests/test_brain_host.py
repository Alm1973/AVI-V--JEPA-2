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


class EveryReplyHasABody(unittest.TestCase):
    def test_every_brain_gesture_is_playable_or_deliberately_nothing(self):
        from avi_bot.brain import schema
        from avi_bot.motion import gestures
        for g in schema.GESTURES:
            with self.subTest(g):
                self.assertIn(g, bh.ARM_GESTURE)
                arm = bh.ARM_GESTURE[g]
                if arm is not None:
                    gestures.make(arm)          # raises if the arm lacks it

    def test_every_beep_has_a_gesture_until_there_is_a_speaker(self):
        from avi_bot.brain import schema
        from avi_bot.motion import gestures
        for e in schema.EMOTIONS:
            with self.subTest(e):
                gestures.make(bh.BEEP_GESTURE[e])

    def test_a_timer_set_nods(self):
        self.assertEqual(bh.body_move(None, "neutral"), "nod")
        self.assertIsNone(bh.body_move(None, "neutral", have_speaker=True))


class SomethingToTellHim(unittest.TestCase):
    def test_a_search_result_waits_until_he_is_there(self):
        here = {"v": False}
        h = bh.Host(lambda: Brain([]), present=lambda: here["v"],
                    note=lambda m: None)
        h.announce("Found your keys. Left of the laptop.")
        self.assertIsNone(h.tick(), "told an empty room")
        here["v"] = True
        self.assertEqual(h.tick(), "Found your keys. Left of the laptop.")
        self.assertIsNone(h.tick())

    def test_it_goes_out_even_before_anyone_has_spoken_to_it(self):
        h = bh.Host(lambda: Brain([]), note=lambda m: None)
        h.announce("Found your keys.")
        self.assertEqual(h.tick(), "Found your keys.")


class WhileYouWereAway(unittest.TestCase):
    """Real scheduler and store: the reminder fires to an empty room, then
    he comes back."""

    def setUp(self):
        from avi_bot import scheduler as sched_mod
        from avi_bot.memory import db as memdb
        self.now = {"t": 1_760_000_000.0}
        clock = lambda: self.now["t"]                         # noqa: E731
        self.store = memdb.Store(clock=clock)
        self.addCleanup(self.store.close)
        sched = sched_mod.Scheduler(store=self.store, clock=clock)

        class RealBrain:
            scheduler = sched

            def tick(self):
                return sched.next_announcement()
        self.here = {"v": False}
        self.host = bh.Host(RealBrain, present=lambda: self.here["v"],
                            note=lambda m: None, clock=clock)
        self.host.brain

    def test_told_when_he_is_back_and_only_once(self):
        self.store.add_reminder(self.now["t"] + 5, "call mom")
        self.now["t"] += 6
        self.assertIsNone(self.host.tick(), "told an empty room")
        self.now["t"] += 3600                  # long past stale
        self.here["v"] = True
        said = self.host.tick()
        self.assertTrue(said.startswith("While you were away at "))
        self.assertTrue(said.endswith(": call mom"))
        self.assertIsNone(self.host.tick())
        self.assertEqual(self.host.brain.scheduler.missed_reminders(), [])

    def test_a_restart_does_not_lose_it(self):
        self.store.add_reminder(self.now["t"] + 5, "call mom")
        self.now["t"] += 6
        self.host.tick()                       # held, he is away
        self.host.pending.clear()              # what a restart does
        self.here["v"] = True
        self.assertIn("call mom", self.host.tick())

    def test_quiet_mode_holds_what_avi_found_too(self):
        self.host.brain.scheduler.muted = lambda: True
        self.here["v"] = True
        self.host.announce("Found your keys.")
        self.assertIsNone(self.host.tick())
        self.host.brain.scheduler.muted = lambda: False
        self.assertEqual(self.host.tick(), "Found your keys.")

    def test_the_dashboard_builds_it_when_the_loop_starts(self):
        built = []
        h = bh.Host(lambda: built.append(1) or Brain([]), note=lambda m: None,
                    eager=True)
        h.loop(deadline=0)
        self.assertEqual(built, [1])


class Arrival(unittest.TestCase):
    def host(self, text):
        class B(Brain):
            def rundown(self):
                return text
        h = bh.Host(lambda: B([]), note=lambda m: None)
        h.brain
        return h

    def test_the_first_sit_down_gets_the_rundown(self):
        h = self.host("Today: call mom at 3:00 PM.")
        self.assertEqual(h.arrived(first_today=True),
                         "Today: call mom at 3:00 PM.")
        self.assertEqual(h.tick(), "Today: call mom at 3:00 PM.")

    def test_coming_back_from_lunch_does_not(self):
        h = self.host("Today: call mom at 3:00 PM.")
        self.assertIsNone(h.arrived(first_today=False))
        self.assertEqual(h.pending, [])

    def test_nothing_ahead_says_nothing(self):
        h = self.host(None)
        h.arrived()
        self.assertEqual(h.pending, [])


class FocusSeesThePhone(unittest.TestCase):
    """focus.py decided well and was never told anything."""

    def setUp(self):
        from avi_bot.skills import focus as focus_mod
        self.fm = focus_mod
        self.t = {"now": 1000.0}
        clock = lambda: self.t["now"]                         # noqa: E731
        self.focus = focus_mod.Focus(clock=clock)

        class B(Brain):
            focus = self.focus
        self.moves = []
        self.h = bh.Host(lambda: B([]), note=lambda m: None, clock=clock,
                         perform=self.moves.append)
        self.h.brain

    def phone_for(self, seconds, step=20.0):
        for _ in range(int(seconds / step) + 1):
            self.h.saw_phone(True, now=self.t["now"])
            self.t["now"] += step

    def test_nothing_happens_without_a_session(self):
        self.phone_for(300)
        self.assertIsNone(self.h.focus_tick(breakpoint=True))
        self.assertEqual(self.moves, [])

    def test_phone_time_then_a_pause_is_a_glance_not_words(self):
        self.focus.start(None)
        self.phone_for(150)
        self.assertIsNone(self.h.focus_tick(breakpoint=False))
        self.assertEqual(self.h.focus_tick(breakpoint=True), self.fm.AHEM)
        self.assertEqual(self.moves, [bh.CUE_GESTURE])
        self.assertEqual(self.h.pending, [])

    def test_the_second_nudge_has_words(self):
        self.focus.start(None)
        self.phone_for(150)
        self.h.focus_tick(breakpoint=True)
        self.t["now"] += 400
        self.phone_for(150)
        self.assertEqual(self.h.focus_tick(breakpoint=True), self.fm.WORDS)
        self.assertEqual(self.h.tick(), bh.Host.FOCUS_PHONE_WORDS)

    def test_a_timed_session_ends_on_time_with_its_summary(self):
        self.focus.start(25)
        self.t["now"] += 25 * 60
        self.assertEqual(self.h.focus_tick(), "done")
        self.assertFalse(self.h.focus_active())
        self.assertTrue(self.h.tick().startswith("Focus done. 25 minutes"))
