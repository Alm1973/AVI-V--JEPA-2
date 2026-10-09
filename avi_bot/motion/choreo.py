"""Improvised performances: "do an interpretive dance like a dolphin".

THE PIPELINE, from GenEM (Mahadevan et al., HRI 2024; docs/RESEARCH-motion.md):
the language model first pictures how the thing would be done ("a dolphin
arcs out of the water and dives back"), then writes it in the robot's own
PRIMITIVES rather than in raw joint numbers. Composing from primitives is
what made GenEM's behaviours readable, and it is what a 20B model can do
reliably: asked for raw keyframes it has to know four joints' limits, their
signs and the arm's speed, and it does not.

This module is the second half: primitives -> one smooth KeyframeMove that

  * stays inside the soft limits (each joint's offsets are SCALED to fit,
    which keeps the shape -- clipping would flatten the top of every wave);
  * keeps the arm off the desk by the body model (motion/body.py) where that
    model is trusted, by shrinking the reaching joints;
  * fits the arm's real speed (27 deg/s): a segment gets the time its
    biggest change needs, so a fast wiggle becomes a slower visible one
    instead of a smear;
  * ends where it started, so it can be checked from the picture.

`improvise(name)` composes one with no language model at all, from what the
name evokes, so "dance like a dolphin" does something offline too. Either
way the result is kept (memory/db.py learned_moves) and replayed, scaled up
when the camera saw nothing happen, and dropped when he says it was bad.
"""
import math
import re
import zlib

from . import backlash as backlash_mod
from . import body as body_mod
from . import poses
from .engine import KeyframeMove

# Seconds per beat at each tempo. The arm's speed still has the last word.
TEMPO = {"slow": 0.9, "normal": 0.6, "fast": 0.4}
SIZES = {1: 0.5, 2: 1.0, 3: 1.5}
MAX_MOVES = 12
MAX_TIMES = 6
MAX_SECONDS = 30.0
EDGE_DEG = 2.0                     # stay this far inside the soft limits

# Peak speed of an ease_in_out segment is pi/2 of its average (gestures.py).
_PEAK = math.pi / 2


def _cycle(times, frames):
    out = []
    for _ in range(times):
        out.extend(frames)
    return out


def _sway(a, n):
    return _cycle(n, [(1, {"base_yaw": -14 * a}), (1, {"base_yaw": 14 * a})])


def _bob(a, n):
    return _cycle(n, [(1, {"wrist": 10 * a}), (1, {"wrist": -8 * a})])


def _nod(a, n):
    return _cycle(n, [(1, {"wrist": -10 * a}), (1, {"wrist": 3 * a})])


def _shake(a, n):
    return _cycle(n, [(0.7, {"base_yaw": -10 * a}),
                      (0.7, {"base_yaw": 10 * a})])


def _wave(a, n):
    """A travelling wave down the arm: shoulder leads, elbow a quarter
    behind, wrist half behind. The undulation of a swimming body."""
    out = []
    for _ in range(n):
        for k in range(4):
            th = math.pi / 2 * (k + 1)
            out.append((1, {"shoulder": 6 * a * math.sin(th),
                            "elbow": 9 * a * math.sin(th - math.pi / 2),
                            "wrist": 14 * a * math.sin(th - math.pi)}))
    return out


def _leap(a, n):
    """Arc up out of the water, look up, then dive and look down."""
    return _cycle(n, [(1.2, {"shoulder": -8 * a, "elbow": -8 * a,
                             "wrist": 16 * a}),
                      (1.2, {"shoulder": 8 * a, "elbow": 6 * a,
                             "wrist": -14 * a})])


def _spin(a, n):
    return _cycle(n, [(2, {"base_yaw": -30 * a}), (2, {"base_yaw": 30 * a})])


def _wiggle(a, n):
    return _cycle(n, [(0.5, {"base_yaw": 6 * a, "wrist": -4 * a}),
                      (0.5, {"base_yaw": -6 * a, "wrist": 4 * a})])


def _circle(a, n):
    """The camera traces a circle: base and wrist a quarter apart."""
    out = []
    for _ in range(n):
        for k in range(4):
            th = math.pi / 2 * (k + 1)
            out.append((1, {"base_yaw": 12 * a * math.sin(th),
                            "wrist": 10 * a * (math.cos(th) - 1)}))
    return out


def _held(offsets):
    def build(a, n):
        k = {j: v * a for j, v in offsets.items()}
        return [(1, k), (max(1, n), k)]
    return build


def _pause(a, n):
    return [(max(1, n), {})]


# name -> (what it looks like, for the model; builder(size, times))
PRIMITIVES = {
    "sway": ("side to side", _sway),
    "bob": ("camera up and down, like a head bob", _bob),
    "nod": ("a nod", _nod),
    "shake": ("a quick no-shake", _shake),
    "wave": ("a wave rolling down the whole arm, like swimming", _wave),
    "leap": ("arc up and look up, then dive and look down", _leap),
    "spin": ("a big slow turn left then right", _spin),
    "wiggle": ("small quick jiggles", _wiggle),
    "circle": ("the camera traces a circle", _circle),
    "lean_in": ("lean forward toward him", _held({"shoulder": 8,
                                                  "wrist": 6})),
    "rise": ("stretch up tall, looking up", _held({"shoulder": -10,
                                                   "wrist": 12})),
    "duck": ("shrink down, looking down", _held({"shoulder": 8, "elbow": 10,
                                                 "wrist": -10})),
    "look_left": ("turn and look left", _held({"base_yaw": -25})),
    "look_right": ("turn and look right", _held({"base_yaw": 25})),
    "look_up": ("look up", _held({"wrist": 18})),
    "look_down": ("look down", _held({"wrist": -18})),
    "pause": ("hold still for a beat", _pause),
}
NAMES = tuple(PRIMITIVES)


def menu():
    """One line per primitive, for the tool description."""
    return "; ".join(f"{k} ({v[0]})" for k, v in PRIMITIVES.items())


def clean(moves):
    """The moves a model sent, made valid: unknown primitives dropped,
    numbers clamped. Returns (moves, dropped_names)."""
    out, dropped = [], []
    for m in (moves or [])[:MAX_MOVES]:
        if isinstance(m, str):
            m = {"move": m}
        if not isinstance(m, dict):
            continue
        name = str(m.get("move") or m.get("name") or "").strip().lower()
        name = name.replace(" ", "_").replace("-", "_")
        if name not in PRIMITIVES:
            dropped.append(name)
            continue
        try:
            size = int(round(float(m.get("size") or 2)))
        except (TypeError, ValueError):
            size = 2
        try:
            times = int(round(float(m.get("times") or 1)))
        except (TypeError, ValueError):
            times = 1
        speed = str(m.get("speed") or "normal").lower()
        out.append({"move": name, "size": max(1, min(3, size)),
                    "times": max(1, min(MAX_TIMES, times)),
                    "speed": speed if speed in TEMPO else "normal"})
    return out, dropped


def frames(moves, scale=1.0, tempo=1.0):
    """[(seconds_wanted, {joint: offset})], the dance at its intended
    tempo (beats x `tempo`), each primitive ending back at the start pose."""
    out = []
    for m in moves:
        a = SIZES[m["size"]] * float(scale)
        beat = TEMPO[m["speed"]] * float(tempo)
        for beats, off in PRIMITIVES[m["move"]][1](a, m["times"]):
            out.append((beats * beat, dict(off)))
        if out and out[-1][1]:
            out.append((beat, {}))           # back to the start pose
    return out


def _fit_limits(seq, start, config):
    """Keep start+offset inside each joint's soft limits, by SHIFTING the
    joint's offsets toward the side with room, and scaling only when the
    swing is wider than the room. Clipping would flatten the top of every
    wave; scaling alone wasted it -- ATTENTIVE's shoulder sits 2.5 degrees
    above its limit, and a dolphin's arc lost 70% of its shoulder to that
    one side."""
    notes = []
    for j in poses.JOINTS:
        lo, hi = config.soft_limits(j)
        here = float(start[j])
        lo_r, hi_r = lo + EDGE_DEG - here, hi - EDGE_DEG - here
        if hi_r < lo_r:                   # a joint parked at its limit
            lo_r = hi_r = (lo_r + hi_r) / 2.0
        present = [off[j] for _, off in seq if j in off]
        if not present:
            continue
        mn, mx = min(present), max(present)
        if mx - mn > hi_r - lo_r:
            k = (hi_r - lo_r) / (mx - mn) if mx > mn else 0.0
            notes.append(f"{j} x{k:.2f} (limits)")
            for _, off in seq:
                if j in off:
                    off[j] *= k
            mn, mx = mn * k, mx * k
        shift = (lo_r - mn if mn < lo_r else hi_r - mx if mx > hi_r
                 else 0.0)
        if abs(shift) > 1e-9:
            notes.append(f"{j} {shift:+.1f} (limits)")
            for _, off in seq:
                if j in off:
                    off[j] += shift
    return notes


def _fit_body(seq, start, body):
    """Shrink the reaching joints until no frame takes a part near the
    desk. Only where the model holds: a start it already calls unsafe is a
    shape it does not describe (REST, folded), and there it says nothing."""
    if body is None or not body.safe(start):
        return []
    notes = []
    for _ in range(4):
        bad = [off for _, off in seq
               if not body.safe(dict(start, **{
                   j: start[j] + off.get(j, 0.0) for j in poses.JOINTS}))]
        if not bad:
            return notes
        for _, off in seq:
            for j in ("shoulder", "elbow"):
                if j in off:
                    off[j] *= 0.5
        notes.append("reach x0.5 (desk)")
    for _, off in seq:
        off.pop("shoulder", None)
        off.pop("elbow", None)
    notes.append("reach dropped (desk)")
    return notes


def _timed(seq, rate):
    """Each segment gets at least the time its biggest change needs at
    `rate` deg/s on an ease_in_out curve."""
    out, prev = [], {}
    for want, off in seq:
        big = max((abs(off.get(j, 0.0) - prev.get(j, 0.0))
                   for j in poses.JOINTS), default=0.0)
        out.append((max(want, big * _PEAK / rate if rate > 0 else want),
                    off))
        prev = off
    return out


def compose(moves, start, config=None, body=None, scale=1.0, rate=None,
            name="perform", style=None):
    """(KeyframeMove, info) for `moves` played from pose `start`.

    info: seconds, frames, notes (what was changed to make it safe), and
    the clamped `moves`. Raises ValueError when nothing valid is left.

    `style` is the mood's {"speed", "amplitude"} (personality/mood.py):
    folded in HERE, before the dance is fitted to the limits and the arm's
    speed. Applied by the engine afterwards, an excited 1.15x tempo and
    1.1x size went past both.
    """
    config = config or poses.load()
    if body is None:
        body = body_mod.load()
    moves, dropped = clean(moves)
    if not moves:
        raise ValueError("no moves I know" + (
            f" (not {', '.join(d for d in dropped if d)})" if dropped
            else ""))
    rate = float(rate or getattr(config, "achievable", None)
                 or poses.load().achievable)
    start = {j: float(start[j]) for j in poses.JOINTS}
    style = style or {}
    speed = max(0.3, float(style.get("speed", 1.0)))
    # Most of a dance runs at the arm's speed ceiling, so a sleepy tempo
    # only reads as slow if the ceiling it is timed against comes down too.
    # An excited one cannot go above it.
    rate *= min(1.0, speed)
    seq = frames(moves, scale=scale * float(style.get("amplitude", 1.0)),
                 tempo=1.0 / speed)
    notes = _fit_limits(seq, start, config)
    notes += _fit_body(seq, start, body)
    seq = _timed(seq, rate)
    total = sum(t for t, _ in seq)
    while total > MAX_SECONDS and len(seq) > 2:
        t, _ = seq.pop()
        total -= t
        notes.append("cut to 30 s")
    if seq and seq[-1][1]:
        t = max(0.4, max(abs(v) for v in seq[-1][1].values()) * _PEAK / rate)
        seq.append((t, {}))
        total += t
    # Every joint has slack (motion/backlash.py: the base ~18 degrees, the
    # others ~1-1.5): a dance that comes home from the wrong side stops
    # short, and on the robot the first dances ended 13 degrees (base) and
    # 23-39 px (pitch) off their start (2026-10-09). So it comes home the
    # way every move arrives: a run-up past the start on each joint the
    # dance used, then the last approach from its ARRIVE side.
    used = [j for j in poses.JOINTS
            if any(abs(off.get(j, 0.0)) > 1e-6 for _, off in seq)]
    dip = {}
    for j in used:
        pre = backlash_mod.run_up(j, start[j], config.soft_limits(j))
        if pre is not None:
            dip[j] = pre - start[j]
    if dip:
        t = max(0.4, max(abs(v) for v in dip.values()) * _PEAK / rate)
        seq += [(t, dip), (t, {})]
        total += 2 * t
        notes.append("comes home from one side (backlash)")
    keys, t = [(0.0, {})], 0.0
    for dt, off in seq:
        t += dt
        keys.append((min(1.0, t / total), {j: round(v, 2)
                                          for j, v in off.items()}))
    move = KeyframeMove(name, keys, total, ease="ease_in_out", max_rate=rate,
                        fit="stretch")
    return move, {"seconds": round(move.duration, 1), "frames": len(seq),
                  "notes": sorted(set(notes)), "moves": moves,
                  "dropped": [d for d in dropped if d]}


# --- "do it bigger" ------------------------------------------------------
# GenEM's feedback step: a correction edits the behaviour rather than
# starting over. Each word moves every primitive one notch.
ADJUST = ("bigger", "smaller", "faster", "slower")
_TEMPOS = ("slow", "normal", "fast")


def adjust(moves, how):
    """The same dance one notch bigger/smaller/faster/slower."""
    moves, _ = clean(moves)
    out = []
    for m in moves:
        m = dict(m)
        if how in ("bigger", "smaller"):
            m["size"] = max(1, min(3, m["size"] + (1 if how == "bigger"
                                                   else -1)))
        elif how in ("faster", "slower"):
            k = _TEMPOS.index(m["speed"]) + (1 if how == "faster" else -1)
            m["speed"] = _TEMPOS[max(0, min(2, k))]
            if how == "faster":
                # The arm already runs at its 27 deg/s ceiling for most of
                # a dance, so a quicker beat alone took the dolphin from
                # 23.1 s to 22.2 s on the robot. Smaller swings are what
                # actually let it go faster (17.4 s).
                m["size"] = max(1, m["size"] - 1)
        out.append(m)
    return out


# --- with no language model --------------------------------------------
# What a name evokes, in primitives. Checked in order; the first theme whose
# word appears wins. Anything else gets a dance seeded from its own name, so
# the same request always gets the same (remembered) dance.
THEMES = (
    (("dolphin", "fish", "ocean", "sea", "swim", "whale", "shark", "wave",
      "mermaid", "water"),
     ["leap", "wave", "wave", "leap", "sway", "pause"], "normal"),
    (("snake", "worm", "noodle", "jelly", "seaweed", "tentacle"),
     ["wave", "circle", "wave", "sway"], "slow"),
    (("robot", "machine", "mechanical", "computer"),
     ["look_left", "look_right", "nod", "look_up", "shake"], "fast"),
    (("bird", "chicken", "pigeon", "duck", "parrot"),
     ["nod", "nod", "look_left", "look_right", "bob"], "fast"),
    (("cat", "dog", "puppy", "kitten"),
     ["wiggle", "lean_in", "bob", "shake"], "normal"),
    (("sad", "tired", "sleepy", "rain", "lonely", "slow"),
     ["look_down", "sway", "pause", "duck"], "slow"),
    (("happy", "party", "celebrate", "celebration", "excited", "disco",
      "victory", "win"),
     ["wiggle", "sway", "bob", "spin", "wiggle"], "fast"),
    (("bee", "butterfly", "fly", "bug", "hummingbird"),
     ["wiggle", "circle", "wiggle", "look_up", "circle"], "fast"),
    (("horse", "pony", "gallop", "kangaroo", "bunny", "rabbit", "frog"),
     ["bob", "nod", "bob", "leap", "bob"], "fast"),
    (("tree", "wind", "breeze", "grass", "flower", "leaf", "cloud"),
     ["sway", "rise", "sway", "circle"], "slow"),
    (("ballet", "elegant", "graceful", "waltz", "swan"),
     ["rise", "circle", "sway", "look_up", "duck"], "slow"),
    (("zombie", "ghost", "spooky", "monster"),
     ["look_down", "lean_in", "shake", "lean_in", "pause"], "slow"),
    (("monkey", "crazy", "silly", "wild", "chaos"),
     ["wiggle", "shake", "spin", "bob", "wiggle"], "fast"),
)


def improvise(name, variant=0):
    """(moves, idea) for `name` with no language model. `variant` > 0
    gives a different one for the same name: the last was disliked."""
    low = (name or "").lower()
    seed = zlib.crc32(f"{low}#{int(variant)}".encode("utf-8"))
    for words, prims, speed in THEMES:
        # Whole words (a plural too): "win" must not be found in "wind".
        hit = next((w for w in words
                    if re.search(rf"\b{w}(?:s|es)?\b", low)), None)
        if hit:
            prims = list(prims)
            if variant:
                # Same vocabulary, new order and sizes.
                k = seed % len(prims)
                prims = prims[k:] + prims[:k]
            return ([{"move": p,
                      "size": 2 if not variant else 1 + (seed >> i) % 3,
                      "times": 2 if p in ("sway", "wiggle", "nod", "bob")
                      else 1, "speed": speed}
                     for i, p in enumerate(prims)],
                    f"moves that feel like '{hit}'")
    pool = ("sway", "bob", "wiggle", "circle", "leap", "look_left",
            "look_right", "rise", "nod", "spin")
    picks = [pool[(seed >> (4 * i)) % len(pool)] for i in range(5)]
    return ([{"move": p, "size": 2, "times": 2, "speed": "normal"}
             for p in picks], "a dance of my own")
