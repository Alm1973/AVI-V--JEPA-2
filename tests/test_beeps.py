"""The chirp synthesizer. BUILD_SPEC 7.1.

Skipped wholesale when numpy is missing, which is the case on the Mac: the
synth itself is pure arithmetic and runs anywhere numpy does, but there is no
point pretending to test it without one.
"""
import random
import sys
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import numpy as np
    from avi_bot.voice import beeps
except ImportError:                       # pragma: no cover
    beeps = None


@unittest.skipIf(beeps is None, "numpy is not installed here")
class TestSyllable(unittest.TestCase):
    def test_both_ends_reach_zero(self):
        """A tone that starts or stops on a non-zero sample clicks, and a chain
        of clicks is what makes synthesised beeps sound cheap."""
        s = beeps.Syllable(900, 1200, 0.1)
        a = beeps.render_syllable(s)
        self.assertLess(abs(float(a[0])), 1e-3)
        self.assertLess(abs(float(a[-1])), 1e-3)

    def test_a_glide_actually_ends_on_the_target_pitch(self):
        """Phase has to be the integral of frequency. Writing it as f(t)*t
        glides at twice the intended rate and lands on the wrong note."""
        f0, f1, dur = 400.0, 800.0, 0.5
        a = beeps.render_syllable(beeps.Syllable(f0, f1, dur, harmonic=0.0))
        tail = a[int(0.9 * len(a)):int(0.98 * len(a))]
        crossings = np.sum(np.diff(np.signbit(tail)) != 0)
        measured = crossings / (2.0 * len(tail) / beeps.SR)
        self.assertAlmostEqual(measured, f1, delta=f1 * 0.15,
                               msg=f"ends near {measured:.0f} Hz, wanted {f1}")

    def test_nothing_clips(self):
        rng = random.Random(1)
        for name in beeps.PRESETS:
            a = beeps.render(name, rng)
            self.assertLessEqual(float(np.max(np.abs(a))), 1.0, name)

    def test_duration_is_in_the_spec_s_range(self):
        for name, p in beeps.PRESETS.items():
            lo, hi = p["dur"]
            self.assertGreaterEqual(lo, 0.040, name)
            self.assertLessEqual(hi, 0.180, name)


@unittest.skipIf(beeps is None, "numpy is not installed here")
class TestPresets(unittest.TestCase):
    def test_every_preset_in_the_spec_s_table_exists(self):
        for name in ("happy", "excited", "affirm", "deny", "question",
                     "curious", "sad", "sulky", "ahem", "sleepy", "alert"):
            self.assertIn(name, beeps.PRESETS, name)

    def test_every_preset_renders_to_something_audible(self):
        rng = random.Random(2)
        for name in beeps.PRESETS:
            a = beeps.render(name, rng)
            self.assertGreater(len(a), 0.03 * beeps.SR, name)
            self.assertGreater(float(np.max(np.abs(a))), 0.1, name)

    def test_syllable_counts_match_the_table(self):
        rng = random.Random(3)
        for name, p in beeps.PRESETS.items():
            lo, hi = p["n"]
            for _ in range(20):
                self.assertTrue(lo <= len(beeps.phrase(name, rng)) <= hi, name)

    def test_the_same_preset_never_renders_identically_twice(self):
        """BUILD_SPEC: randomise within the ranges on every call."""
        rng = random.Random(4)
        for name in beeps.PRESETS:
            a = beeps.render(name, rng)
            b = beeps.render(name, rng)
            same = len(a) == len(b) and bool(np.allclose(a, b))
            self.assertFalse(same, f"{name} repeated exactly")

    def test_sad_and_sleepy_fall_while_happy_rises(self):
        rng = random.Random(5)
        for name in ("sad", "sleepy"):
            syl = beeps.phrase(name, rng)
            self.assertLess(syl[-1].f1, syl[0].f0, name)
        up = beeps.phrase("affirm", rng)
        self.assertGreater(up[-1].f1, up[0].f0)

    def test_sulky_is_low_and_short(self):
        rng = random.Random(6)
        syl = beeps.phrase("sulky", rng)
        self.assertEqual(len(syl), 1)
        self.assertLess(syl[0].f0, 500)

    def test_alert_is_three_of_the_same_note(self):
        rng = random.Random(7)
        syl = beeps.phrase("alert", rng)
        self.assertEqual(len(syl), 3)
        self.assertAlmostEqual(syl[0].f0, syl[0].f1, places=3)

    def test_the_output_rate_matches_kokoro(self):
        # So speech and chirps share one output path with no resampling.
        self.assertEqual(beeps.SR, 24_000)


@unittest.skipIf(beeps is None, "numpy is not installed here")
class TestPolicy(unittest.TestCase):
    def test_quiet_hours_cover_the_spec_s_window(self):
        for hour in (22, 23, 0, 3, 6):
            self.assertTrue(beeps.is_quiet_hours(hour), hour)
        for hour in (7, 12, 15, 21):
            self.assertFalse(beeps.is_quiet_hours(hour), hour)

    def test_quiet_hours_mute_everything_but_an_urgent_reminder(self):
        for name in beeps.PRESETS:
            self.assertFalse(beeps.should_play(name, hour=2), name)
        self.assertTrue(beeps.should_play("alert", hour=2, urgent=True))
        self.assertFalse(beeps.should_play("happy", hour=2, urgent=True),
                         "urgent is for reminders, not for being cheerful")

    def test_everything_plays_during_the_day(self):
        for name in beeps.PRESETS:
            self.assertTrue(beeps.should_play(name, hour=15), name)

    def test_every_mood_emotion_maps_to_a_chirp_or_to_silence(self):
        from avi_bot.personality import mood as mood_mod
        for emotion in mood_mod.STYLES:
            self.assertIn(emotion, beeps.FOR_EMOTION,
                          f"{emotion} has no chirp mapping")
            name = beeps.for_emotion(emotion)
            if name is not None:
                self.assertIn(name, beeps.PRESETS, f"{emotion} -> {name}")

    def test_content_is_silent_on_purpose(self):
        self.assertIsNone(beeps.for_emotion("content"))


@unittest.skipIf(beeps is None, "numpy is not installed here")
class TestExport(unittest.TestCase):
    def test_export_writes_readable_24k_mono_wavs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            beeps.main(["--export", d, "--takes", "1", "--seed", "11"])
            files = sorted(Path(d).glob("*.wav"))
            self.assertEqual(len(files), len(beeps.PRESETS))
            for f in files:
                with wave.open(str(f), "rb") as w:
                    self.assertEqual(w.getnchannels(), 1, f.name)
                    self.assertEqual(w.getsampwidth(), 2, f.name)
                    self.assertEqual(w.getframerate(), beeps.SR, f.name)
                    self.assertGreater(w.getnframes(), 0, f.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
