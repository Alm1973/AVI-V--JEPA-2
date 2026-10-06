"""Procedural chirps. BUILD_SPEC 7.1.

    python -m avi_bot.voice.beeps --export out/   # render every preset to WAV

Every phrase is a chain of syllables: a sine or triangle tone with some second
harmonic, 40-180 ms long, gliding from one pitch to another, optionally with
vibrato, under an ADSR envelope, separated by 20-80 ms gaps. Each preset
specifies RANGES, and a fresh value is drawn inside them on every call, so the
same emotion never chirps identically twice.

Nothing is sampled. BUILD_SPEC is explicit about this: no `ttastromech`, which
bundles sound files of unclear origin, and no movie sound effects. Everything
here is generated from numpy arithmetic, so there is no licence attached to any
of it.

Output is 24 kHz mono float in [-1, 1], matching Kokoro's rate so speech and
chirps can share one output path without resampling.
"""
import argparse
import math
import os
import random
import struct
import sys
import wave

import numpy as np

SR = 24_000          # same as Kokoro, so nothing has to be resampled
ATTACK_S = 0.008     # long enough to avoid a click, short enough to stay crisp
RELEASE_S = 0.025


class Syllable:
    """One tone. Pitch glides f0 -> f1 over `dur` seconds."""

    def __init__(self, f0, f1, dur, wave_kind="sine", harmonic=0.25,
                 vibrato_hz=0.0, vibrato_cents=0.0, level=1.0):
        self.f0 = float(f0)
        self.f1 = float(f1)
        self.dur = float(dur)
        self.wave_kind = wave_kind
        self.harmonic = float(harmonic)
        self.vibrato_hz = float(vibrato_hz)
        self.vibrato_cents = float(vibrato_cents)
        self.level = float(level)


def _envelope(n, sr=SR):
    """ADSR, flattened to attack-sustain-release at these durations.

    A separate decay stage is pointless for a 40 ms tone, and the thing that
    actually matters is that both ends reach zero: a tone that starts or stops
    at a non-zero sample clicks, and a chain of clicks is what makes
    synthesised beeps sound cheap.
    """
    env = np.ones(n, dtype=np.float32)
    a = min(int(ATTACK_S * sr), n // 2)
    r = min(int(RELEASE_S * sr), n - a)
    if a > 0:
        env[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32) ** 0.6
    if r > 0:
        env[n - r:] = np.linspace(1.0, 0.0, r, dtype=np.float32) ** 1.4
    return env


def render_syllable(s, sr=SR):
    n = max(1, int(s.dur * sr))
    t = np.arange(n, dtype=np.float32) / sr

    # Instantaneous frequency glides linearly; the phase is its integral, which
    # has to be accumulated rather than written as f*t -- using f(t)*t gives a
    # glide at twice the intended rate and a tone that ends on the wrong pitch.
    f = np.linspace(s.f0, s.f1, n, dtype=np.float32)
    if s.vibrato_hz > 0 and s.vibrato_cents > 0:
        f = f * (2.0 ** (s.vibrato_cents / 1200.0
                         * np.sin(2 * np.pi * s.vibrato_hz * t)))
    phase = 2 * np.pi * np.cumsum(f, dtype=np.float32) / sr

    if s.wave_kind == "tri":
        # Triangle from the phase directly, so it cannot alias the way a naive
        # harmonic sum does at these frequencies.
        base = 2.0 / np.pi * np.arcsin(np.sin(phase))
    elif s.wave_kind == "square":
        # Softened, not a hard sign(): a true square at 400 Hz through a small
        # speaker is harsh rather than sulky.
        base = np.tanh(2.5 * np.sin(phase))
    else:
        base = np.sin(phase)

    tone = base + s.harmonic * np.sin(2 * phase)
    tone /= (1.0 + s.harmonic)
    return (tone * _envelope(n, sr) * s.level).astype(np.float32)


# --- the presets ------------------------------------------------------------
# Straight from BUILD_SPEC 7.1's table. `n` is the syllable count range, `f`
# the pitch range, and `contour` says what the glide does.
PRESETS = {
    "happy":    {"n": (2, 3), "f": (900, 1600), "contour": "rise",
                 "dur": (0.07, 0.11), "wave": "sine"},
    "excited":  {"n": (4, 6), "f": (1000, 2200), "contour": "trill",
                 "dur": (0.04, 0.07), "wave": "sine"},
    "affirm":   {"n": (2, 2), "f": (800, 1200), "contour": "up",
                 "dur": (0.06, 0.09), "wave": "sine"},
    "deny":     {"n": (2, 2), "f": (700, 1100), "contour": "down",
                 "dur": (0.07, 0.10), "wave": "tri"},
    "question": {"n": (1, 2), "f": (700, 1400), "contour": "rise_end",
                 "dur": (0.09, 0.14), "wave": "sine"},
    "curious":  {"n": (1, 2), "f": (700, 1400), "contour": "rise_end",
                 "dur": (0.09, 0.14), "wave": "sine"},
    "sad":      {"n": (2, 2), "f": (400, 700), "contour": "down",
                 "dur": (0.14, 0.18), "wave": "sine"},
    "sulky":    {"n": (1, 1), "f": (300, 450), "contour": "flat",
                 "dur": (0.10, 0.15), "wave": "square"},
    "ahem":     {"n": (2, 2), "f": (500, 650), "contour": "up",
                 "dur": (0.08, 0.11), "wave": "tri"},
    "sleepy":   {"n": (1, 1), "f": (300, 600), "contour": "down",
                 "dur": (0.16, 0.18), "wave": "sine"},
    "alert":    {"n": (3, 3), "f": (1300, 1300), "contour": "flat",
                 "dur": (0.07, 0.09), "wave": "sine", "level": 1.0},
}

# Mapping from a mood emotion to a chirp, so the personality engine does not
# need its own table. `content` has no chirp of its own: a content robot is
# quiet, which is the point.
FOR_EMOTION = {
    "happy": "happy", "excited": "excited", "curious": "curious",
    "sad": "sad", "sulky": "sulky", "sleepy": "sleepy",
    "bored": "question", "content": None,
}

GAP_S = (0.020, 0.080)
QUIET_FROM, QUIET_TO = 22, 7


def _pitches(preset, n, rng):
    lo, hi = preset["f"]
    c = preset["contour"]
    if c == "flat":
        f = rng.uniform(lo, hi)
        return [(f, f)] * n
    if c == "up" or c == "rise":
        step = (hi - lo) / max(1, n)
        out = []
        for i in range(n):
            a = lo + step * i
            out.append((a, min(hi, a + step * 0.9)))
        return out
    if c == "down":
        step = (hi - lo) / max(1, n)
        out = []
        for i in range(n):
            a = hi - step * i
            out.append((a, max(lo, a - step * 0.9)))
        return out
    if c == "rise_end":
        out = [(rng.uniform(lo, (lo + hi) / 2),) * 2 for _ in range(max(0, n - 1))]
        out = [(a, b) for a, b in out]
        out.append((rng.uniform(lo, (lo + hi) / 2), hi))
        return out
    if c == "trill":
        out = []
        for i in range(n):
            a = rng.uniform(lo, hi)
            b = rng.uniform(lo, hi)
            out.append((a, b))
        return out
    raise ValueError(f"unknown contour {c!r}")


def phrase(name, rng=None):
    """Build one realisation of a preset as a list of Syllables."""
    if name not in PRESETS:
        raise KeyError(f"unknown beep {name!r}; have {sorted(PRESETS)}")
    rng = rng or random.Random()
    p = PRESETS[name]
    n = rng.randint(*p["n"])
    out = []
    for f0, f1 in _pitches(p, n, rng):
        out.append(Syllable(
            f0=f0, f1=f1, dur=rng.uniform(*p["dur"]),
            wave_kind=p["wave"],
            harmonic=rng.uniform(0.15, 0.35),
            vibrato_hz=rng.uniform(4.0, 7.0) if name == "excited" else 0.0,
            vibrato_cents=25.0 if name == "excited" else 0.0,
            level=p.get("level", 0.9)))
    return out


def render(name, rng=None, volume=1.0, sr=SR):
    """Render a preset to a float array in [-1, 1]."""
    rng = rng or random.Random()
    parts = []
    syllables = phrase(name, rng)
    for i, s in enumerate(syllables):
        parts.append(render_syllable(s, sr))
        if i < len(syllables) - 1:
            gap = int(rng.uniform(*GAP_S) * sr)
            parts.append(np.zeros(gap, dtype=np.float32))
    audio = np.concatenate(parts) * float(volume)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.98:
        audio = audio * (0.98 / peak)
    return audio.astype(np.float32)


def is_quiet_hours(hour):
    return hour >= QUIET_FROM or hour < QUIET_TO


def should_play(name, hour, urgent=False):
    """Quiet hours mute everything except a reminder flagged urgent."""
    if not is_quiet_hours(hour):
        return True
    return bool(urgent) and name == "alert"


def for_emotion(emotion):
    return FOR_EMOTION.get(emotion)


def write_wav(path, audio, sr=SR):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    pcm = np.clip(audio, -1.0, 1.0)
    data = (pcm * 32767.0).astype("<i2").tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)
    return len(pcm) / float(sr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--export", metavar="DIR",
                    help="render every preset to WAV so they can be listened to")
    ap.add_argument("--takes", type=int, default=3,
                    help="how many different realisations of each preset")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--volume", type=float, default=0.9)
    args = ap.parse_args(argv)

    if not args.export:
        ap.error("nothing to do; pass --export DIR")

    rng = random.Random(args.seed)
    total = 0.0
    for name in sorted(PRESETS):
        for take in range(1, args.takes + 1):
            audio = render(name, rng, volume=args.volume)
            path = os.path.join(args.export, f"{name}_{take}.wav")
            secs = write_wav(path, audio)
            total += secs
            print(f"  {os.path.basename(path):22} {secs * 1000:5.0f} ms  "
                  f"peak {np.max(np.abs(audio)):.2f}")
    print(f"\n{len(PRESETS)} presets x {args.takes} takes -> {args.export} "
          f"({total:.1f}s of audio)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
