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


if __name__ == "__main__":
    unittest.main()
