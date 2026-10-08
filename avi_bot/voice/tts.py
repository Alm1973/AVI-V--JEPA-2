"""Turning AVI's twenty words into sound. BUILD_SPEC 7.2.

THE BACKEND IS NOT A PREFERENCE, IT IS WHAT INSTALLS
BUILD_SPEC asks for Kokoro-82M with Piper as the fallback. Measured
2026-10-05: the MSI's venv runs Python 3.13.13 and Kokoro needs < 3.13, so
Kokoro does not install there at all. That is a version wall, not a speed
problem, and it is the reason this module picks a backend at runtime from
what is actually importable instead of hard-coding one.

MEASURED on the MSI 2026-10-05, en_US-amy-medium at 22050 Hz, CPU only:
**RTF 0.036-0.039 across 2 to 15 word lines**, i.e. about 26x faster than
real time, 4.3 s to load the model once. So Piper is not a compromise and
Kokoro is not needed: a twenty-word reply synthesises in under a quarter of a
second, which is well inside the time it takes the arm to finish the gesture
that goes with it.

Order: Kokoro (if a < 3.13 interpreter ever exists here), Piper (measured
above, the primary), then Silent.
Silent is a real backend, not an error: a robot whose voice fails should go
back to chirping, which it does well, rather than stop acknowledging him.

REAL-TIME FACTOR IS THE ACCEPTANCE TEST
BUILD_SPEC: "If Kokoro is slower than real time on this CPU, switch to Piper."
So every backend reports RTF = synthesis seconds / audio seconds, and anything
above 1.0 cannot keep up with speech. With a 20-word cap an utterance is about
six seconds of audio, so even RTF 0.5 means three seconds of waiting before
the first word -- which is why `stream()` synthesises sentence by sentence and
hands back the first one as soon as it exists.
"""
import math
import time

SAMPLE_RATE = 24000          # matches beeps.py, so one output path serves both
MAX_RTF = 1.0                # above this a backend cannot keep up with speech


class Spoken:
    """Audio plus the numbers needed to judge the backend."""

    __slots__ = ("samples", "rate", "synth_s", "backend", "text")

    def __init__(self, samples, rate, synth_s, backend, text):
        self.samples = samples
        self.rate = rate
        self.synth_s = float(synth_s)
        self.backend = backend
        self.text = text

    @property
    def audio_s(self):
        return len(self.samples) / float(self.rate) if self.rate else 0.0

    @property
    def rtf(self):
        """Synthesis time over audio time. Below 1.0 keeps up with speech."""
        return self.synth_s / self.audio_s if self.audio_s else float("inf")

    @property
    def keeps_up(self):
        return self.rtf <= MAX_RTF

    def __repr__(self):
        return (f"<Spoken {self.backend} {self.audio_s:.2f}s audio "
                f"rtf {self.rtf:.2f}{'' if self.keeps_up else ' TOO SLOW'}>")


def ring_modulate(samples, rate=SAMPLE_RATE, hz=55.0, depth=0.22):
    """A subtle robotic tint, behind a setting (BUILD_SPEC 7.2).

    Kept gentle and kept optional. Depth 1.0 is the classic Dalek and makes
    speech genuinely harder to understand, which is a poor trade for twenty
    words that only get spoken when they carry information.
    """
    if not samples or depth <= 0.0:
        return samples
    out = []
    step = 2.0 * math.pi * hz / float(rate)
    for i, s in enumerate(samples):
        out.append(s * (1.0 - depth + depth * math.sin(step * i)))
    return out


def split_sentences(text, max_words=20):
    """Sentences, so synthesis can start before the whole reply is ready."""
    if not text:
        return []
    out, current = [], []
    for word in text.split():
        current.append(word)
        if word[-1:] in ".!?" or len(current) >= max_words:
            out.append(" ".join(current))
            current = []
    if current:
        out.append(" ".join(current))
    return out


# --- backends -------------------------------------------------------------
class Silent:
    """No voice available. Not an error: fall back to chirps."""

    name = "silent"
    available = True

    def synth(self, text):
        return [], SAMPLE_RATE


class Kokoro:
    name = "kokoro"

    def __init__(self, voice="af_heart"):
        self.voice = voice
        self._pipe = None

    @property
    def available(self):
        try:
            import kokoro            # noqa: F401
        except Exception:
            return False
        return True

    def synth(self, text):
        if self._pipe is None:
            from kokoro import KPipeline
            self._pipe = KPipeline(lang_code="a")
        chunks = []
        for _, _, audio in self._pipe(text, voice=self.voice):
            chunks.extend(audio.tolist() if hasattr(audio, "tolist")
                          else list(audio))
        return chunks, 24000


class Piper:
    name = "piper"

    def __init__(self, model=None):
        # A path, or a callable returning one: the dashboard passes a lookup
        # of its "voice" setting, so switching voices needs no restart.
        self.model = model
        self._voice = None
        self._loaded = None

    @property
    def available(self):
        try:
            import piper             # noqa: F401
        except Exception:
            return False
        return True

    def synth(self, text):
        import io
        import wave
        from piper import PiperVoice
        path = self.model() if callable(self.model) else self.model
        if not path:
            raise RuntimeError("piper needs a voice model path")
        if self._voice is None or path != self._loaded:
            self._voice = PiperVoice.load(path)
            self._loaded = path
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            self._voice.synthesize_wav(text, wav)
        buf.seek(0)
        with wave.open(buf, "rb") as wav:
            rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
        samples = [int.from_bytes(raw[i:i + 2], "little", signed=True) / 32768.0
                   for i in range(0, len(raw) - 1, 2)]
        return samples, rate


class Voice:
    def __init__(self, backends=None, robotic=False, clock=time.perf_counter):
        self.backends = list(backends if backends is not None
                             else (Kokoro(), Piper(), Silent()))
        self.robotic = robotic
        self.clock = clock
        self._chosen = None
        self.rtf_seen = {}

    def backend(self):
        """First importable backend. Decided once, then remembered."""
        if self._chosen is None:
            for candidate in self.backends:
                try:
                    if candidate.available:
                        self._chosen = candidate
                        break
                except Exception:
                    continue
            else:
                self._chosen = Silent()
        return self._chosen

    def say(self, text):
        """Synthesise one utterance, timing it."""
        backend = self.backend()
        started = self.clock()
        try:
            samples, rate = backend.synth(text)
        except Exception:
            # A backend that imports but fails at synthesis is worse than one
            # that was never there, because it fails per utterance. Demote it
            # permanently rather than retrying every sentence.
            self.backends = [b for b in self.backends if b is not backend]
            self._chosen = None
            if self.backends:
                return self.say(text)
            samples, rate = [], SAMPLE_RATE
            backend = Silent()
        took = self.clock() - started
        if self.robotic and samples:
            samples = ring_modulate(samples, rate)
        spoken = Spoken(samples, rate, took, backend.name, text)
        self.rtf_seen.setdefault(backend.name, []).append(spoken.rtf)
        return spoken

    def stream(self, text, max_words=20):
        """Sentence by sentence, so the first word does not wait for the last."""
        for sentence in split_sentences(text, max_words=max_words):
            yield self.say(sentence)

    def too_slow(self, backend_name=None):
        """Has this backend failed to keep up, on average?

        BUILD_SPEC's rule for switching away from Kokoro, as a measurement
        rather than an impression.
        """
        name = backend_name or self.backend().name
        seen = [r for r in self.rtf_seen.get(name, []) if math.isfinite(r)]
        if not seen:
            return None
        return (sum(seen) / len(seen)) > MAX_RTF
