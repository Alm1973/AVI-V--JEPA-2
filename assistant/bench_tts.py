"""Measure the voice's real-time factor. BUILD_SPEC 7.2.

"If Kokoro is slower than real time on this CPU, switch to Piper." Kokoro
will not install here at all (this venv is Python 3.13 and Kokoro needs
< 3.13), so the question becomes whether Piper keeps up -- and that has to be
a measurement, because the whole speech policy assumes a reply of twenty words
or fewer arrives while it is still relevant.

RTF = synthesis seconds / audio seconds. Below 1.0 keeps up with speech.
Writes no audio file unless asked.
"""
import argparse
import io
import sys
import time
import wave

LINES = [
    "Quarter past nine.",
    "Your phone was on the desk at four.",
    "Timer finished.",
    "Three things today: chemistry at ten, the dentist at two, and dinner "
    "with your parents.",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="en_US-amy-medium")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--out", default=None, help="write the last line to a WAV")
    args = ap.parse_args()

    try:
        from piper import PiperVoice
    except Exception as exc:
        print(f"piper unavailable: {exc}")
        return 1

    t0 = time.perf_counter()
    try:
        voice = PiperVoice.load(args.model)
    except Exception:
        # Newer piper-tts resolves a bare voice name only after download.
        try:
            from pathlib import Path
            from piper.download_voices import download_voice
            # Wants a Path, not a str: it does `dir / name` internally.
            download_voice(args.model, Path("."))
            voice = PiperVoice.load(f"{args.model}.onnx")
        except Exception as exc:
            print(f"could not load or fetch {args.model}: "
                  f"{type(exc).__name__}: {exc}")
            return 1
    print(f"loaded {args.model} in {time.perf_counter() - t0:.1f}s")

    worst = 0.0
    for line in LINES:
        rtfs = []
        for _ in range(args.runs):
            buf = io.BytesIO()
            s = time.perf_counter()
            with wave.open(buf, "wb") as w:
                voice.synthesize_wav(line, w)
            took = time.perf_counter() - s
            buf.seek(0)
            with wave.open(buf, "rb") as w:
                audio_s = w.getnframes() / float(w.getframerate())
                rate = w.getframerate()
            rtfs.append(took / audio_s if audio_s else float("inf"))
        best = min(rtfs)
        worst = max(worst, best)
        print(f"  {len(line.split()):>2} words  {audio_s:4.2f}s audio  "
              f"rtf {best:.3f}  {'ok' if best <= 1.0 else 'TOO SLOW'}"
              f"  ({rate} Hz)")
        if args.out and line is LINES[-1]:
            with open(args.out, "wb") as f:
                f.write(buf.getvalue())
            print(f"  wrote {args.out}")
    print(f"\nworst rtf {worst:.3f} -> "
          f"{'Piper keeps up with speech' if worst <= 1.0 else 'too slow'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
