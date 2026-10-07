"""AVI's voice and chirps, out of a real speaker.

The voice (tts.py, Piper, measured 26x faster than real time on the MSI) and
the chirp language (beeps.py) both existed and produced audio that went
nowhere: nothing played it. The robot's own speakers land Thursday, but the
MSI has speakers today, and Windows can play a WAV from memory with the
standard library (winsound), so nothing has to be installed.

OFF unless switched on (dashboard-settings.json "speak"): a laptop that
starts talking in his room is his call. When on, beeps.py's quiet hours still
mute chirps at night, and the speech policy upstream still caps what is said.
"""
import io
import threading
import time
import wave

import numpy as np


def to_wav(samples, rate):
    """float samples in [-1, 1] -> 16-bit mono WAV bytes."""
    a = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (a * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm)
    return buf.getvalue()


def winsound_play(wav_bytes):
    """Blocking playback from memory. Windows only, standard library."""
    import winsound
    winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)


class Speaker:
    def __init__(self, voice=None, play=winsound_play, enabled=lambda: False,
                 hour=lambda: time.localtime().tm_hour, note=print):
        self.voice = voice
        self.play = play
        self.enabled = enabled
        self.hour = hour
        self.note = note
        # One sound at a time; a chirp must not talk over a sentence.
        self._lock = threading.Lock()

    def say(self, text):
        """Speak, sentence by sentence. Returns True if anything played."""
        if not text or not self.enabled() or self.voice is None:
            return False
        played = False
        with self._lock:
            for spoken in self.voice.stream(text):
                if spoken.samples:
                    self.play(to_wav(spoken.samples, spoken.rate))
                    played = True
        return played

    def chirp(self, emotion, urgent=False):
        """The beep for an emotion. Returns True if it played."""
        from avi_bot.voice import beeps
        if not self.enabled():
            return False
        name = beeps.for_emotion(emotion)
        if name is None or not beeps.should_play(name, self.hour(), urgent):
            return False
        with self._lock:
            self.play(to_wav(beeps.render(name), beeps.SR))
        return True

    def say_later(self, text):
        """Speak without holding up the caller (an HTTP reply, a tick)."""
        threading.Thread(target=self._safely, args=(self.say, text),
                         daemon=True).start()

    def chirp_later(self, emotion):
        threading.Thread(target=self._safely, args=(self.chirp, emotion),
                         daemon=True).start()

    def _safely(self, fn, arg):
        try:
            fn(arg)
        except Exception as exc:
            self.note(f"speaker: {exc!r}")
