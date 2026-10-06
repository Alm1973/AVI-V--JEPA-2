"""Lamp, media keys and the four mode switches.

All three touch the world, so all three inject the part that does. What gets
tested is the behaviour that matters when the world misbehaves: a plug that
does not answer, a keyboard that is not reachable, and guest mode failing
closed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.brain import speech                              # noqa: E402
from avi_bot.skills import lamp as lamp_mod                   # noqa: E402
from avi_bot.skills import media as media_mod                 # noqa: E402
from avi_bot.skills import modes as modes_mod                 # noqa: E402
from avi_bot.skills import registry                           # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s
        return self.t


class LampTests(unittest.TestCase):
    def setUp(self):
        self.state = {"on": False}
        self.calls = []

        def transport(action, host, creds):
            self.calls.append((action, host))
            if action == "on":
                self.state["on"] = True
            elif action == "off":
                self.state["on"] = False
            else:
                self.state["on"] = not self.state["on"]
            return self.state["on"]

        self.lamp = lamp_mod.Lamp(host="10.0.0.5", transport=transport)

    def test_on_and_off(self):
        self.assertEqual(self.lamp.set("on")[0], "Lamp on.")
        self.assertEqual(self.lamp.set("off")[0], "Lamp off.")

    def test_toggle_follows_the_real_state(self):
        """No optimistic cache: a lamp believed on while actually off makes
        toggle flip the wrong way, and the robot argues with the room."""
        self.state["on"] = True
        text, data = self.lamp.set("toggle")
        self.assertFalse(data["state"])
        self.assertEqual(text, "Lamp off.")

    def test_a_silent_plug_is_reported_not_guessed(self):
        def dead(action, host, creds):
            raise OSError("no route to host")
        lamp = lamp_mod.Lamp(host="10.0.0.5", transport=dead)
        text, data = lamp.set("on")
        self.assertIn("didn't answer", text)
        self.assertIsNone(data["state"])

    def test_no_host_configured(self):
        lamp = lamp_mod.Lamp(host=None, transport=lambda *a: True)
        self.assertIn("No lamp", lamp.set("on")[0])

    def test_a_bad_action_is_refused(self):
        self.assertIn("not on, off or toggle", self.lamp.set("disco")[0])
        self.assertEqual(self.calls, [])

    def test_the_sentence_fits_the_speech_cap(self):
        for action in ("on", "off", "toggle"):
            text, _ = self.lamp.set(action)
            self.assertLessEqual(len(text.split()), speech.MAX_WORDS)


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.pressed = []
        self.media = media_mod.Media(send=self.pressed.append)

    def test_each_action_sends_its_key(self):
        for action, vk in (("play_pause", 0xB3), ("next", 0xB0),
                           ("previous", 0xB1), ("mute", 0xAD)):
            self.pressed.clear()
            self.media.do(action)
            self.assertEqual(self.pressed, [vk], action)

    def test_aliases_map_to_real_keys(self):
        for alias, vk in (("pause", 0xB3), ("play", 0xB3), ("stop", 0xB3),
                          ("skip", 0xB0), ("back", 0xB1)):
            self.pressed.clear()
            text, data = self.media.do(alias)
            self.assertEqual(self.pressed, [vk], alias)

    def test_volume_is_repeated_so_the_change_is_noticeable(self):
        """Windows moves volume ~2% a press: one is imperceptible, twenty is a
        shock."""
        self.media.do("volume_down")
        self.assertEqual(len(self.pressed), media_mod.REPEATS["volume_down"])
        self.assertTrue(all(vk == 0xAE for vk in self.pressed))

    def test_an_unknown_action_sends_nothing(self):
        text, data = self.media.do("scratch the record")
        self.assertIsNone(data)
        self.assertEqual(self.pressed, [])

    def test_media_actions_say_nothing(self):
        """Plainly visible and audible, so a chirp -- the speech policy would
        drop a sentence here anyway."""
        text, data = self.media.do("next")
        self.assertIsNone(text)
        self.assertEqual(data["action"], "next")

    def test_an_unreachable_keyboard_is_reported(self):
        def dead(vk):
            raise OSError("session 0 has no desktop")
        m = media_mod.Media(send=dead)
        text, data = m.do("next")
        self.assertIn("Couldn't reach", text)


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.poses = []
        self.logging = []
        self.policy = speech.SpeechPolicy()
        self.clock = Clock()
        self.m = modes_mod.Modes(clock=self.clock,
                                 on_pose=self.poses.append,
                                 on_logging=self.logging.append,
                                 policy=self.policy)

    def test_sleep_rests_and_stops_logging(self):
        self.m.sleep()
        self.assertEqual(self.poses, [modes_mod.REST])
        self.assertEqual(self.logging, [False])
        self.assertFalse(self.m.may_log)

    def test_wake_resumes(self):
        self.m.sleep()
        self.m.wake()
        self.assertEqual(self.logging, [False, True])
        self.assertTrue(self.m.may_log)

    def test_waking_does_not_quietly_undo_guest_mode(self):
        self.m.guest_mode(True)
        self.m.sleep()
        self.m.wake()
        self.assertFalse(self.m.may_log, "logging resumed with a guest here")

    def test_guest_mode_fails_closed(self):
        """The camera points at someone who did not agree to be recorded.
        Missing an hour of observations costs nothing."""
        self.m.guest_mode(True)
        self.assertFalse(self.m.may_log)
        self.assertEqual(self.logging[-1], False)

    def test_guest_mode_toggles(self):
        self.m.guest_mode()
        self.assertTrue(self.m.guest)
        self.m.guest_mode()
        self.assertFalse(self.m.guest)

    def test_quiet_silences_the_policy(self):
        self.m.quiet(10)
        self.assertTrue(self.m.is_quiet)
        out = self.policy.apply(
            __import__("avi_bot.brain.schema", fromlist=["Reply"]).Reply(
                say="Quarter past nine."), speech.ANSWER)
        self.assertIsNone(out.say)

    def test_quiet_expires_without_anyone_calling_unquiet(self):
        self.m.quiet(10)
        self.clock.tick(601)
        self.assertFalse(self.m.is_quiet)
        self.assertFalse(self.policy.quiet, "stayed silent after the timer")

    def test_quiet_with_no_length_is_indefinite(self):
        self.m.quiet()
        self.clock.tick(10 ** 6)
        self.assertTrue(self.m.is_quiet)
        self.m.unquiet()
        self.assertFalse(self.m.is_quiet)

    def test_snapshot_reports_everything(self):
        self.m.guest_mode(True)
        self.m.quiet(5)
        snap = self.m.snapshot()
        self.assertEqual(snap, {"asleep": False, "guest": True,
                                "quiet": True, "may_log": False})

    def test_all_four_wire_into_the_registry(self):
        reg = registry.Registry()
        for name, fn in modes_mod.make(self.m).items():
            reg.register(name, fn)
        for name, args in (("sleep", {}), ("wake", {}),
                           ("quiet", {"minutes": 5}), ("guest_mode", {})):
            self.assertTrue(reg.run(name, args).ok, name)


class Wiring(unittest.TestCase):
    def test_lamp_and_media_wire_in(self):
        reg = registry.Registry()
        lamp = lamp_mod.Lamp(host="x", transport=lambda *a: True)
        media = media_mod.Media(send=lambda vk: None)
        for name, fn in {**lamp_mod.make(lamp),
                         **media_mod.make(media)}.items():
            reg.register(name, fn)
        self.assertTrue(reg.run("lamp", {"action": "on"}).ok)
        self.assertTrue(reg.run("media", {"action": "next"}).ok)
        self.assertFalse(reg.run("media", {"action": "rewind"}).ok,
                         "the schema should reject an unlisted action")


if __name__ == "__main__":
    unittest.main()
