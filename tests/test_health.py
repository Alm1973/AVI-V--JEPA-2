"""One answer per part of AVI: running, off, stale or broken."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.dashboard import health                               # noqa: E402

NOW = 1_791_330_000.0


class Health(unittest.TestCase):
    def test_all_running(self):
        h = health.check(NOW, presence_last_ts=NOW - 15, latents_on=True,
                         latent_last_ts=NOW - 10, brain_started=True,
                         ends_in_s=3600)
        self.assertEqual(h["summary"], "all running")

    def test_latents_switched_off_is_said_plainly(self):
        h = health.check(NOW, presence_last_ts=NOW - 15)
        self.assertEqual(h["latents"]["state"], "off")

    def test_a_silent_loop_is_stale(self):
        h = health.check(NOW, presence_last_ts=NOW - 600, latents_on=True,
                         latent_last_ts=NOW - 900)
        self.assertEqual(h["summary"], "check: presence, latents")

    def test_broken_brain_is_named(self):
        h = health.check(NOW, presence_last_ts=NOW, brain_error="no keys")
        self.assertIn("brain", h["summary"])



class Voice(unittest.TestCase):
    def test_off_is_off(self):
        h = health.check(NOW, presence_last_ts=NOW, voice={"on": False})
        self.assertEqual(h["voice"]["state"], "off")

    def test_the_silent_fallback_is_broken_not_quiet(self):
        h = health.check(NOW, presence_last_ts=NOW,
                         voice={"on": True, "backend": "silent", "said": 0})
        self.assertEqual(h["voice"]["state"], "broken")
        self.assertIn("voice", h["summary"])

    def test_piper_speaking_is_ok(self):
        h = health.check(NOW, presence_last_ts=NOW,
                         voice={"on": True, "backend": "piper", "said": 3})
        self.assertEqual(h["voice"], {"state": "ok", "said": 3,
                                      "backend": "piper"})


class ArmLink(unittest.TestCase):
    def test_a_dead_serial_link_is_broken(self):
        h = health.check(NOW, presence_last_ts=NOW,
                         arm_link="could not open port 'COM5'")
        self.assertEqual(h["arm"]["state"], "broken")
        self.assertIn("arm", h["summary"])

    def test_ok_is_ok(self):
        h = health.check(NOW, presence_last_ts=NOW, arm_link="ok")
        self.assertEqual(h["arm"], {"state": "ok"})

if __name__ == "__main__":
    unittest.main()



class TheDailySelfCheck(unittest.TestCase):
    def test_a_stuck_joint_is_broken(self):
        from avi_bot.dashboard import health
        got = health.check(1000.0, selfcheck={
            "day": "2026-10-09", "joints": {
                "elbow": {"ok": False, "ratio": 0.05},
                "wrist": {"ok": True, "ratio": 0.9}}})
        self.assertEqual(got["joints"]["state"], "broken")
        self.assertIn("elbow", got["joints"]["why"])
        self.assertIn("joints", got["summary"])

    def test_all_moving_is_ok(self):
        from avi_bot.dashboard import health
        got = health.check(1000.0, selfcheck={
            "day": "2026-10-09", "joints": {"wrist": {"ok": True}}})
        self.assertEqual(got["joints"]["state"], "ok")
