"""Generate WAV fixtures for the hearing tests, with TTS.

BUILD_SPEC 7.3: "Generate fixtures with TTS so they contain no personal
audio." That is the whole reason this script exists rather than a microphone:
the hearing tests need real speech to transcribe, and recording Shaurya saying
twenty phrases would put his voice in the repo forever.

Writes 16 kHz mono, which is what faster-whisper and openWakeWord both want.
"""
import argparse
import os
import wave

LINES = {
    "wake_hey_jarvis": "Hey Jarvis.",
    "wake_hey_jarvis_then_command": "Hey Jarvis, set a timer for five minutes.",
    "cmd_timer": "Set a timer for five minutes.",
    "cmd_lamp_off": "Turn the lamp off.",
    "cmd_where_phone": "Where is my phone?",
    "cmd_focus": "Focus for twenty five minutes.",
    "cmd_schedule": "What is on my schedule today?",
    "cmd_time": "What time is it?",
    "no_wake_word": "The weather is quite warm this afternoon.",
    "near_miss_jarvis": "I was reading about Jarvis in a comic book.",
}


def resample_to_16k(samples, rate):
    """Nearest-neighbour down to 16 kHz. Crude on purpose: these are fixtures
    for a recogniser, not audio anyone listens to, and a dependency-free
    resample keeps the generator runnable anywhere Piper is."""
    if rate == 16000:
        return samples, 16000
    step = rate / 16000.0
    out = [samples[int(i * step)] for i in range(int(len(samples) / step))]
    return out, 16000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="en_US-amy-medium")
    ap.add_argument("--out", default="../fixtures/audio")
    args = ap.parse_args()

    from piper import PiperVoice
    try:
        voice = PiperVoice.load(args.model)
    except Exception:
        from pathlib import Path
        from piper.download_voices import download_voice
        download_voice(args.model, Path("."))
        voice = PiperVoice.load(f"{args.model}.onnx")

    os.makedirs(args.out, exist_ok=True)
    import io
    for name, text in LINES.items():
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w)
        buf.seek(0)
        with wave.open(buf, "rb") as w:
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
        samples = [int.from_bytes(raw[i:i + 2], "little", signed=True)
                   for i in range(0, len(raw) - 1, 2)]
        samples, rate = resample_to_16k(samples, rate)
        path = os.path.join(args.out, f"{name}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"".join(
                int(s).to_bytes(2, "little", signed=True) for s in samples))
        print(f"  {path}  {len(samples) / rate:.2f}s  {rate} Hz")
    # A manifest, so the tests know what each file is meant to contain without
    # anyone having to listen to them.
    with open(os.path.join(args.out, "manifest.txt"), "w") as f:
        for name, text in LINES.items():
            f.write(f"{name}\t{text}\n")
    print(f"wrote {len(LINES)} fixtures + manifest")


if __name__ == "__main__":
    main()
