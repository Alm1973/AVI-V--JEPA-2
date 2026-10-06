"""Hearing: the echo gate, the WAV test path, and wake-word dispatch.

BUILD_SPEC 7.3's tests are "WAV fixtures transcribe" and "the wake word fires
on its fixture". The reSpeaker arrives Thursday and neither openWakeWord nor
faster-whisper is installed, so the model calls are stubbed and what gets
tested is everything around them -- which is where the one failure that makes
the robot unusable lives.

The fixtures are TTS-generated (fixtures/audio/, from Piper) so no personal
audio is in the repo, per the spec.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.hearing import listener as hear                  # noqa: E402

AUDIO = Path(__file__).resolve().parents[1] / "fixtures" / "audio"


def manifest():
    out = {}
    for line in (AUDIO / "manifest.txt").read_text().splitlines():
        if "\t" in line:
            name, text = line.split("\t", 1)
            out[name] = text
    return out


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s
        return self.t


def saying(text):
    """A transcriber that always returns `text`, and a wake detector that
    fires when the manifest says the clip contains the wake word."""
    return (lambda s, r: text)


class Fixtures(unittest.TestCase):
    def test_they_exist_and_are_the_right_format(self):
        names = manifest()
        self.assertGreaterEqual(len(names), 10)
        for name in names:
            path = AUDIO / f"{name}.wav"
            self.assertTrue(path.exists(), name)
            samples, rate = hear.read_wav(path)
            self.assertEqual(rate, hear.SAMPLE_RATE,
                             f"{name}: both oWW and faster-whisper want 16 kHz")
            self.assertGreater(len(samples) / rate, 0.5, name)

    def test_no_personal_audio_in_the_repo(self):
        """Every fixture is accounted for in the manifest, i.e. generated."""
        wavs = {p.stem for p in AUDIO.glob("*.wav")}
        self.assertEqual(wavs - set(manifest()), set(),
                         "a WAV with no manifest entry: where did it come from?")

    def test_a_fixture_transcribes_through_the_wav_path(self):
        want = manifest()["cmd_timer"]
        lis = hear.Listener(transcribe=saying(want), clock=Clock())
        self.assertEqual(lis.transcribe_wav(AUDIO / "cmd_timer.wav"), want)

    def test_mono_16bit_is_enforced(self):
        import wave
        bad = AUDIO / "_stereo_tmp.wav"
        try:
            with wave.open(str(bad), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00" * 64)
            with self.assertRaises(ValueError):
                hear.read_wav(bad)
        finally:
            bad.unlink(missing_ok=True)


class TheEchoGate(unittest.TestCase):
    """The one failure that makes the robot unusable: AVI answers itself."""

    def setUp(self):
        self.clock = Clock()
        self.lis = hear.Listener(detect_wake=lambda s, r: 1.0,
                                 transcribe=saying("what time is it"),
                                 clock=self.clock)

    def test_it_hears_when_quiet(self):
        heard = self.lis.offer([0] * 16000)
        self.assertIsNotNone(heard)
        self.assertEqual(heard.text, "what time is it")

    def test_it_is_deaf_while_speaking(self):
        self.lis.speaking(True)
        self.assertTrue(self.lis.deaf)
        self.assertIsNone(self.lis.offer([0] * 16000))

    def test_it_stays_deaf_for_the_echo_tail(self):
        """The speaker is still moving and the input buffer still holds it."""
        self.lis.speaking(True)
        self.lis.speaking(False)
        self.assertTrue(self.lis.deaf)
        self.clock.tick(hear.ECHO_TAIL_S / 2)
        self.assertTrue(self.lis.deaf)
        self.assertIsNone(self.lis.offer([0] * 16000))

    def test_it_hears_again_after_the_tail(self):
        self.lis.speaking(True)
        self.lis.speaking(False)
        self.clock.tick(hear.ECHO_TAIL_S + 0.01)
        self.assertFalse(self.lis.deaf)
        self.assertIsNotNone(self.lis.offer([0] * 16000))

    def test_the_tail_is_three_hundred_milliseconds(self):
        self.assertAlmostEqual(hear.ECHO_TAIL_S, 0.3)

    def test_dropped_wake_words_are_counted(self):
        """A gate that is too eager looks exactly like a wake word that does
        not work, and this number is the only way to tell them apart later."""
        self.lis.speaking(True)
        for _ in range(3):
            self.lis.offer([0] * 16000)
        self.assertEqual(self.lis.ignored, 3)


class WakeWord(unittest.TestCase):
    def test_it_fires_on_its_fixture(self):
        fired = []

        def detect(samples, rate):
            fired.append(len(samples))
            return 0.92
        lis = hear.Listener(detect_wake=detect,
                            transcribe=saying("hey jarvis"), clock=Clock())
        heard = lis.offer_wav(AUDIO / "wake_hey_jarvis.wav")
        self.assertIsNotNone(heard)
        self.assertGreater(heard.confidence, 0.9)
        self.assertTrue(fired)

    def test_a_low_score_does_not_fire(self):
        lis = hear.Listener(detect_wake=lambda s, r: 0.2,
                            transcribe=saying("nope"), clock=Clock())
        self.assertIsNone(lis.offer_wav(AUDIO / "no_wake_word.wav"))

    def test_nothing_is_transcribed_when_the_wake_word_misses(self):
        """Transcription is the expensive half; it must not run on every
        second of room noise."""
        calls = []
        lis = hear.Listener(detect_wake=lambda s, r: 0.1,
                            transcribe=lambda s, r: calls.append(1) or "x",
                            clock=Clock())
        lis.offer_wav(AUDIO / "no_wake_word.wav")
        self.assertEqual(calls, [], "transcribed without a wake word")

    def test_the_threshold_is_adjustable(self):
        lis = hear.Listener(detect_wake=lambda s, r: 0.6,
                            transcribe=saying("ok"), clock=Clock())
        self.assertIsNotNone(lis.offer([0] * 16000))
        lis.wake_threshold = 0.8
        self.assertIsNone(lis.offer([0] * 16000))

    def test_the_default_wake_word_is_the_pretrained_one(self):
        """openWakeWord's "hey jarvis" until a custom "hey AVI" at M7."""
        self.assertEqual(hear.WAKE_WORD, "hey jarvis")


class StrippingTheWakeWord(unittest.TestCase):
    def test_it_comes_off_the_front(self):
        self.assertEqual(
            hear.strip_wake_word("Hey Jarvis, set a timer for five minutes"),
            "set a timer for five minutes")

    def test_case_and_punctuation(self):
        for raw in ("hey jarvis set a timer", "Hey Jarvis. Set a timer",
                    "HEY JARVIS, set a timer", "hey jarvis  set a timer"):
            self.assertEqual(hear.strip_wake_word(raw).lower(),
                             "set a timer", raw)

    def test_a_mention_mid_sentence_is_left_alone(self):
        text = "I was reading about Jarvis in a comic book"
        self.assertEqual(hear.strip_wake_word(text), text)

    def test_the_stripped_text_routes_correctly(self):
        """The whole point: what comes out of hearing goes into the router."""
        from avi_bot.brain import intents
        cases = {
            "wake_hey_jarvis_then_command": ("set_timer", {"seconds": 300}),
            "cmd_lamp_off": ("lamp", {"action": "off"}),
            "cmd_where_phone": ("find_object", {"name": "phone"}),
            "cmd_focus": ("focus_start", {"minutes": 25}),
            "cmd_time": ("get_time", {}),
        }
        said = manifest()
        for name, (tool, args) in cases.items():
            text = hear.strip_wake_word(said[name])
            intent = intents.match(text)
            self.assertIsNotNone(intent, f"{name}: {text!r} did not route")
            self.assertEqual((intent.tool, intent.args), (tool, args), name)

    def test_empty_and_none(self):
        self.assertEqual(hear.strip_wake_word(""), "")
        self.assertEqual(hear.strip_wake_word(None), "")
        self.assertEqual(hear.strip_wake_word("hey jarvis"), "")


if __name__ == "__main__":
    unittest.main()
