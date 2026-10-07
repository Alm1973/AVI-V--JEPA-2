"""AVI's voice reaches a speaker -- only when switched on."""
import io
import sys
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.voice import output                                    # noqa: E402


class Spoken:
    def __init__(self, text):
        self.samples, self.rate, self.text = [0.1, -0.1] * 100, 22050, text


class FakeVoice:
    def stream(self, text):
        return [Spoken(s) for s in text.split(". ")]


def speaker(on, hour=14):
    played = []
    return output.Speaker(voice=FakeVoice(), play=played.append,
                          enabled=lambda: on, hour=lambda: hour,
                          note=lambda m: None), played


class Speaking(unittest.TestCase):
    def test_off_by_default_says_nothing(self):
        sp, played = speaker(False)
        self.assertFalse(sp.say("Found your keys."))
        self.assertEqual(played, [])

    def test_on_plays_each_sentence(self):
        sp, played = speaker(True)
        self.assertTrue(sp.say("Found your keys. Left of the laptop."))
        self.assertEqual(len(played), 2)
        with wave.open(io.BytesIO(played[0])) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(),
                              w.getframerate()), (1, 2, 22050))

    def test_chirps_respect_quiet_hours(self):
        sp, played = speaker(True, hour=2)
        self.assertFalse(sp.chirp("happy"))
        sp, played = speaker(True, hour=14)
        self.assertTrue(sp.chirp("happy"))
        self.assertEqual(len(played), 1)


class TheHostUsesIt(unittest.TestCase):
    def test_with_a_voice_a_beep_needs_no_nod(self):
        from avi_bot.dashboard import brain_host as bh
        self.assertIsNone(bh.body_move(None, "neutral", have_speaker=True))


if __name__ == "__main__":
    unittest.main()
