"""A switch turned on stays on across a restart."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.dashboard import settings                         # noqa: E402


class Survives(unittest.TestCase):
    def setUp(self):
        self.p = os.path.join(tempfile.mkdtemp(), "s.json")

    def test_nothing_saved_is_empty(self):
        self.assertEqual(settings.load(self.p), {})

    def test_a_switch_is_remembered(self):
        settings.save(self.p, "latents", True)
        self.assertTrue(settings.load(self.p)["latents"])
        settings.save(self.p, "latents", False)
        self.assertFalse(settings.load(self.p)["latents"])

    def test_other_switches_are_kept(self):
        settings.save(self.p, "a", 1)
        settings.save(self.p, "b", 2)
        self.assertEqual(settings.load(self.p), {"a": 1, "b": 2})

    def test_a_broken_file_does_not_stop_startup(self):
        with open(self.p, "w") as f:
            f.write("{not json")
        self.assertEqual(settings.load(self.p), {})


class TheDashboardUsesIt(unittest.TestCase):
    def test_server_restores_and_saves_latents(self):
        src = (Path(__file__).resolve().parents[1]
               / "avi_bot/dashboard/server.py").read_text()
        self.assertIn('settings_mod.load(settings_mod.path()).get("latents")',
                      src)
        self.assertIn('settings_mod.save(settings_mod.path(), "latents"', src)


if __name__ == "__main__":
    unittest.main()
