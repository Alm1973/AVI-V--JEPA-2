"""Voice selection, timing and the robotic tint. No synthesis needed.

The real backends cannot run on the Mac (no Kokoro on 3.14, no voice model),
so everything that is actually DECIDED here is tested with fake backends:
which one gets picked, what happens when one breaks mid-sentence, and whether
the real-time-factor rule BUILD_SPEC asks for is computed correctly.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.voice import tts                                 # noqa: E402


class Fake:
    def __init__(self, name, available=True, audio_s=1.0, rate=24000,
                 explode=False):
        self.name = name
        self.available = available
        self.audio_s = audio_s
        self.rate = rate
        self.explode = explode
        self.calls = 0

    def synth(self, text):
        self.calls += 1
        if self.explode:
            raise RuntimeError("voice model missing")
        return [0.0] * int(self.audio_s * self.rate), self.rate


class Clock:
    def __init__(self, step=0.25):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


class Choosing(unittest.TestCase):
    def test_the_first_available_backend_wins(self):
        a, b = Fake("kokoro", available=False), Fake("piper")
        v = tts.Voice(backends=[a, b])
        self.assertEqual(v.backend().name, "piper")

    def test_silent_is_a_real_fallback_not_an_error(self):
        """A robot whose voice fails should go back to chirping, not stop
        acknowledging him."""
        v = tts.Voice(backends=[Fake("kokoro", available=False)])
        self.assertEqual(v.backend().name, "silent")
        spoken = v.say("hello")
        self.assertEqual(spoken.samples, [])

    def test_a_backend_that_breaks_is_demoted_permanently(self):
        """It imports but fails per utterance, which is worse than absent."""
        broken, good = Fake("piper", explode=True), Fake("espeak")
        v = tts.Voice(backends=[broken, good])
        spoken = v.say("hello there")
        self.assertEqual(spoken.backend, "espeak")
        v.say("again")
        self.assertEqual(broken.calls, 1, "retried a known-broken backend")

    def test_everything_broken_still_returns(self):
        v = tts.Voice(backends=[Fake("piper", explode=True)])
        spoken = v.say("hello")
        self.assertEqual(spoken.backend, "silent")


class RealTimeFactor(unittest.TestCase):
    def test_rtf_is_synthesis_over_audio(self):
        # Clock advances 0.25 s per call, so synth takes 0.25 s for 1 s audio.
        v = tts.Voice(backends=[Fake("piper", audio_s=1.0)], clock=Clock(0.25))
        spoken = v.say("a sentence")
        self.assertAlmostEqual(spoken.rtf, 0.25, places=2)
        self.assertTrue(spoken.keeps_up)

    def test_slower_than_real_time_is_flagged(self):
        """BUILD_SPEC's rule for abandoning Kokoro, as a measurement."""
        v = tts.Voice(backends=[Fake("kokoro", audio_s=0.5)], clock=Clock(1.0))
        spoken = v.say("a sentence")
        self.assertGreater(spoken.rtf, 1.0)
        self.assertFalse(spoken.keeps_up)
        self.assertTrue(v.too_slow("kokoro"))

    def test_too_slow_is_none_before_any_measurement(self):
        v = tts.Voice(backends=[Fake("piper")])
        self.assertIsNone(v.too_slow("piper"))

    def test_empty_audio_does_not_divide_by_zero(self):
        v = tts.Voice(backends=[tts.Silent()])
        spoken = v.say("hello")
        self.assertEqual(spoken.rtf, float("inf"))
        self.assertFalse(spoken.keeps_up)


class Splitting(unittest.TestCase):
    def test_sentences_split_on_punctuation(self):
        self.assertEqual(tts.split_sentences("Found it. On the desk."),
                         ["Found it.", "On the desk."])

    def test_a_long_run_is_broken_at_the_word_cap(self):
        text = " ".join(f"w{i}" for i in range(45))
        parts = tts.split_sentences(text, max_words=20)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p.split()) <= 20 for p in parts))

    def test_streaming_yields_each_sentence(self):
        v = tts.Voice(backends=[Fake("piper")])
        out = list(v.stream("One. Two. Three."))
        self.assertEqual([s.text for s in out], ["One.", "Two.", "Three."])

    def test_empty_text(self):
        self.assertEqual(tts.split_sentences(""), [])
        self.assertEqual(tts.split_sentences(None), [])


class Robotic(unittest.TestCase):
    def test_the_tint_changes_the_audio(self):
        plain = [0.5] * 2400
        tinted = tts.ring_modulate(list(plain), depth=0.22)
        self.assertNotEqual(plain, tinted)

    def test_depth_zero_is_a_no_op(self):
        plain = [0.5] * 100
        self.assertEqual(tts.ring_modulate(list(plain), depth=0.0), plain)

    def test_the_tint_never_clips(self):
        """Depth is subtle on purpose: 1.0 is the classic Dalek and makes
        twenty words that carry information harder to understand."""
        loud = [1.0] * 2400
        out = tts.ring_modulate(loud, depth=0.22)
        self.assertLessEqual(max(abs(s) for s in out), 1.0 + 1e-9)

    def test_it_is_off_unless_asked_for(self):
        v = tts.Voice(backends=[Fake("piper", audio_s=0.1)])
        self.assertFalse(v.robotic)


if __name__ == "__main__":
    unittest.main()


class Tuning(unittest.TestCase):
    """Faster and higher, without Piper having a pitch control."""

    def test_defaults_and_junk_are_the_voice_as_recorded(self):
        self.assertEqual(tts.voice_tuning(None), (1.0, 1.0))
        self.assertEqual(tts.voice_tuning({"speed": "fast"}), (1.0, 1.0))

    def test_clamped_to_speech(self):
        self.assertEqual(tts.voice_tuning({"speed": 9, "pitch": 0.1}),
                         (tts.SPEED_RANGE[1], tts.PITCH_RANGE[0]))

    def test_the_pitch_speed_up_is_paid_back(self):
        """Overall speed = pitch / length_scale, whatever the pitch."""
        for speed, pitch in ((1.15, 1.12), (1.0, 1.2), (0.9, 1.0)):
            ls = tts.length_scale(speed, pitch)
            self.assertAlmostEqual(pitch / ls, speed)
