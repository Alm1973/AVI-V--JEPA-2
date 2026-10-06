"""Hearing: wake word, voice activity, transcription. BUILD_SPEC 7.3.

THE ECHO GATE IS THE PART THAT HAS TO BE RIGHT
AVI speaks through the same reSpeaker it listens on. Without a gate, "Hey
Jarvis, what time is it?" gets answered, the answer is heard, and a robot that
talks to itself is both ridiculous and a loop that never ends. BUILD_SPEC's
rule: ignore the wake word while AVI is playing sound, PLUS 300 ms after, which
covers the tail of the speaker and the buffer still in flight.

The reSpeaker's own echo cancellation needs playback to go through it so it has
a reference signal, which is a hardware wiring requirement -- but AEC is never
perfect and this gate costs nothing, so both are used. Belt and braces on the
one failure that makes the robot unusable rather than merely wrong.

EVERYTHING IS INJECTED, AND FOR ONE REASON
The reSpeaker arrives on Thursday. openWakeWord's models are CC BY-NC-SA
(fine for personal use) and faster-whisper wants a model download; neither is
installed yet. So the gate, the dispatch and the WAV test path are written and
tested now against stubs, and only the model calls are left to plug in. A
design that could not be tested before the hardware existed would have waited
a week for nothing.
"""
import time
import wave

ECHO_TAIL_S = 0.3              # BUILD_SPEC 7.3
WAKE_WORD = "hey jarvis"       # openWakeWord pretrained; "hey AVI" at M7
SAMPLE_RATE = 16000            # what both faster-whisper and oWW want


class Heard:
    __slots__ = ("text", "at", "confidence", "source")

    def __init__(self, text, at, confidence=1.0, source="mic"):
        self.text = text
        self.at = at
        self.confidence = confidence
        self.source = source

    def __repr__(self):
        return f"<Heard {self.text!r} conf={self.confidence:.2f}>"


def read_wav(path):
    """16-bit mono PCM as a list of ints, plus the rate."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"{path}: want 16-bit mono")
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    return [int.from_bytes(raw[i:i + 2], "little", signed=True)
            for i in range(0, len(raw) - 1, 2)], rate


class Listener:
    def __init__(self, detect_wake=None, transcribe=None, clock=time.time,
                 echo_tail_s=ECHO_TAIL_S, wake_word=WAKE_WORD):
        # detect_wake(samples, rate) -> confidence 0..1
        # transcribe(samples, rate) -> text
        self.detect_wake = detect_wake or (lambda s, r: 0.0)
        self.transcribe = transcribe or (lambda s, r: "")
        self.clock = clock
        self.echo_tail_s = echo_tail_s
        self.wake_word = wake_word
        self._speaking_until = 0.0
        self._speaking = False
        self.wake_threshold = 0.5
        self.ignored = 0            # wake words dropped by the echo gate

    # --- the echo gate ---------------------------------------------------
    def speaking(self, on):
        """Tell the listener whether AVI is making sound right now.

        Called by the audio output path. On the falling edge the gate stays
        shut for the echo tail, because the speaker is still moving and the
        input buffer still holds what it said.
        """
        self._speaking = bool(on)
        if not on:
            self._speaking_until = self.clock() + self.echo_tail_s

    @property
    def deaf(self):
        """True while the wake word must be ignored."""
        return self._speaking or self.clock() < self._speaking_until

    # --- hearing ---------------------------------------------------------
    def offer(self, samples, rate=SAMPLE_RATE):
        """Feed audio. Returns a Heard if the wake word fired, else None."""
        if self.deaf:
            # Counted, not silently dropped: a gate that is too eager looks
            # identical to a wake word that does not work, and the only way to
            # tell them apart afterwards is this number.
            self.ignored += 1
            return None
        confidence = float(self.detect_wake(samples, rate) or 0.0)
        if confidence < self.wake_threshold:
            return None
        text = (self.transcribe(samples, rate) or "").strip()
        return Heard(text, self.clock(), confidence=confidence)

    def offer_wav(self, path, rate=None):
        """Test mode: a WAV file instead of the mic (BUILD_SPEC 7.3)."""
        samples, file_rate = read_wav(path)
        return self.offer(samples, rate or file_rate)

    def transcribe_wav(self, path):
        """Transcribe without the wake word, for checking the ASR alone."""
        samples, rate = read_wav(path)
        return (self.transcribe(samples, rate) or "").strip()


def strip_wake_word(text, wake_word=WAKE_WORD):
    """'Hey Jarvis, set a timer' -> 'set a timer'.

    The router's own rules already tolerate a leading wake word, but the
    transcript is also logged and handed to a model, and leaving it on makes
    every utterance in the log start with the same two words.
    """
    if not text:
        return ""
    cleaned = " ".join(text.split())
    lowered = cleaned.lower()
    head = wake_word.lower()
    if lowered.startswith(head):
        cleaned = cleaned[len(head):]
    return cleaned.lstrip(" ,.!?").strip()
