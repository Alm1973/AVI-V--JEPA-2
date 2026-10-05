"""The 50 Hz motion engine: layers, cross-fades, mood styling, clamp, send.

    final = clamp(base + primary + sum(secondary))      -- BUILD_SPEC 6.2

One primary move runs at a time, out of a queue. Pushing a move while another
runs cross-fades between them over 0.3 s rather than snapping. Secondary layers
(breathing, saccades, look-at, speech bob) are weighted and summed on top.

The engine never opens a serial port and never sleeps. It is driven by whoever
calls tick(), so the same code runs the sim at 1000x real time in a test and
the arm at exactly 50 Hz in production.
"""
import math
import random

from . import easing, poses, safety

CROSSFADE_S = 0.3


# --- moves ------------------------------------------------------------------
def _zero():
    return {j: 0.0 for j in poses.JOINTS}


class Move:
    """A primary move.

    sample(elapsed) returns (base_or_None, delta). A move that returns a base is
    changing where the arm rests; a move that returns only a delta is a gesture
    on top of wherever it already rests.
    """
    name = "move"
    duration = 0.0
    continuous = False

    def start(self, base):
        """Called once when the move becomes primary, with the current base."""

    def sample(self, elapsed):
        # Zeroed, never {}: a bare `{}` KeyErrors the moment the engine sums
        # layers, which is how HoldMove broke curious_lean.
        return None, _zero()

    def done(self, elapsed):
        return not self.continuous and elapsed >= self.duration


class KeyframeMove(Move):
    """Per-joint offset curves through keyframes at fractions of the duration.

    keys = [(0.0, {...}), (0.5, {...}), (1.0, {...})], values in degrees of
    offset. Missing joints are 0. Interpolation between frames uses `ease`.
    """

    def __init__(self, name, keys, duration, ease="ease_in_out", max_rate=None):
        self.name = name
        self.duration = float(duration)
        self.keys = sorted(((float(t), dict(v)) for t, v in keys),
                           key=lambda kv: kv[0])
        if not self.keys:
            raise ValueError(f"{name}: no keyframes")
        if self.keys[0][0] != 0.0:
            raise ValueError(f"{name}: first keyframe must be at 0.0 so the "
                             "gesture starts from where the arm already is")
        self.ease_name = ease
        self.ease = easing.get(ease)
        self.scale = 1.0
        if max_rate:
            self._fit(float(max_rate))

    def peak_rate(self):
        """Steepest per-joint rate this curve asks for, deg/s."""
        mult = easing.peak_of(self.ease_name)
        worst = 0.0
        for (ta, va), (tb, vb) in zip(self.keys, self.keys[1:]):
            span = (tb - ta) * self.duration
            if span <= 0:
                continue
            for j in set(va) | set(vb):
                worst = max(worst,
                            abs(vb.get(j, 0.0) - va.get(j, 0.0)) / span * mult)
        return worst

    def _fit(self, max_rate):
        """Shrink the amplitudes until the peak fits, keeping the timing.

        Timing is what makes a gesture read as that gesture -- a nod's rhythm
        is the nod. Amplitude is only how far it goes, and BUILD_SPEC calls the
        amplitudes starting points. So when the arm is too slow, this gives up
        size rather than rhythm.
        """
        peak = self.peak_rate()
        if peak <= max_rate or peak <= 0:
            return
        self.scale = max_rate / peak
        self.keys = [(ta, {j: v * self.scale for j, v in va.items()})
                     for ta, va in self.keys]

    def sample(self, elapsed):
        frac = 1.0 if self.duration <= 0 else min(1.0, elapsed / self.duration)
        prev = self.keys[0]
        for key in self.keys:
            if key[0] >= frac:
                nxt = key
                break
            prev = key
        else:
            nxt = self.keys[-1]
        span = nxt[0] - prev[0]
        w = 0.0 if span <= 0 else self.ease((frac - prev[0]) / span)
        out = _zero()
        for j in poses.JOINTS:
            a = prev[1].get(j, 0.0)
            b = nxt[1].get(j, 0.0)
            out[j] = a + (b - a) * w
        return None, out


class PoseMove(Move):
    """Ease the base pose to a named or explicit pose.

    "Anticipation then overshoot" is not hand-built here: ease_in_out_back dips
    below the start and past the target on its own, which is exactly what
    perk_up and yawn_wake are described as doing.
    """

    def __init__(self, name, target, duration, ease="ease_in_out",
                 max_rate=None):
        self.name = name
        self.target = poses.get_pose(target) if isinstance(target, str) else dict(target)
        self.base_duration = float(duration)
        self.duration = float(duration)
        self.ease_name = ease
        self.ease = easing.get(ease)
        self.max_rate = float(max_rate) if max_rate else None
        self._from = None

    def start(self, base):
        self._from = dict(base)
        # A pose move MUST arrive at its pose, so it cannot give up amplitude
        # the way a keyframe curve can. It gives up time instead, and only as
        # much as the actual travel needs -- which is not knowable until the
        # move starts, because it depends on where the arm already is.
        self.duration = self.base_duration
        if self.max_rate:
            travel = max(abs(self.target[j] - base[j]) for j in poses.JOINTS)
            need = travel * easing.peak_of(self.ease_name) / self.max_rate
            if need > self.duration:
                self.duration = need

    def sample(self, elapsed):
        if self._from is None:          # sampled without start(); be harmless
            return dict(self.target), _zero()
        frac = 1.0 if self.duration <= 0 else min(1.0, elapsed / self.duration)
        w = self.ease(frac)
        base = {j: self._from[j] + (self.target[j] - self._from[j]) * w
                for j in poses.JOINTS}
        return base, _zero()


class HoldMove(Move):
    """Do nothing for a while. Used inside sequences, and as `curious_lean`'s hold."""

    def __init__(self, duration, name="hold"):
        self.name = name
        self.duration = float(duration)


class SequenceMove(Move):
    """Run moves back to back as one primary move, e.g. celebrate."""

    def __init__(self, name, moves):
        self.name = name
        self.moves = list(moves)
        self._i = 0
        self._t0 = 0.0
        self._base = None
        self._carry = _zero()

    @property
    def duration(self):
        """Summed live, not cached.

        A PoseMove leg stretches its own duration at start() when the arm is
        too slow for the travel, so a total computed once at construction goes
        stale and the sequence ends while a leg is still moving.
        """
        return sum(m.duration for m in self.moves)

    def start(self, base):
        self._i = 0
        self._t0 = 0.0
        self._base = dict(base)
        self._carry = _zero()
        if self.moves:
            self.moves[0].start(self._base)

    def sample(self, elapsed):
        base = self._base
        while self._i < len(self.moves):
            m = self.moves[self._i]
            local = elapsed - self._t0
            if local < m.duration or self._i == len(self.moves) - 1:
                b, d = m.sample(local)
                if b is not None:
                    base = b
                    self._base = b
                # Carry the offset the finished legs ended on. Without this,
                # every leg boundary snaps the offset back to zero: that is
                # what made curious_lean's "hold" hold nothing and spike to
                # 3600 deg/s at the seam.
                return base, {j: self._carry[j] + d[j] for j in poses.JOINTS}
            b, d = m.sample(m.duration)
            if b is not None:
                self._base = b
                base = b
            self._carry = {j: self._carry[j] + d[j] for j in poses.JOINTS}
            self._t0 += m.duration
            self._i += 1
            if self._i < len(self.moves):
                self.moves[self._i].start(self._base)
        return base, dict(self._carry)


# --- secondary layers -------------------------------------------------------
class Layer:
    name = "layer"
    weight = 1.0

    def sample(self, now, style):
        return _zero()


class Breathing(Layer):
    """A slow sine on shoulder and elbow. 0.1-0.2 Hz, 1-2 degrees."""
    name = "breathing"

    def __init__(self, freq=0.15, amp=1.5, phase=0.0):
        self.freq = freq
        self.amp = amp
        self.phase = phase
        self.depth = 1.0          # doze raises this

    def sample(self, now, style):
        out = _zero()
        a = self.amp * self.depth * style.amplitude
        v = math.sin(2 * math.pi * self.freq * now + self.phase) * a
        out["shoulder"] = v
        out["elbow"] = v * 0.6
        return out


class Saccades(Layer):
    """Small random yaw/pitch offsets every 1-4 s, as in Stack-chan."""
    name = "saccades"

    def __init__(self, sigma=1.6, min_gap=1.0, max_gap=4.0, rng=None,
                 settle=0.25):
        self.sigma = sigma
        self.min_gap = min_gap
        self.max_gap = max_gap
        self.rng = rng or random.Random()
        self.settle = settle
        self._next = None
        self._from = _zero()
        self._to = _zero()
        self._t0 = 0.0

    def _reroll(self, now):
        self._from = dict(self._to)
        self._to = _zero()
        self._to["base_yaw"] = self.rng.gauss(0.0, self.sigma)
        self._to["wrist"] = self.rng.gauss(0.0, self.sigma * 0.5)
        self._t0 = now
        self._next = now + self.rng.uniform(self.min_gap, self.max_gap)

    def sample(self, now, style):
        if self._next is None or now >= self._next:
            self._reroll(now)
        w = 1.0 if self.settle <= 0 else min(1.0, (now - self._t0) / self.settle)
        w = easing.ease_out(w)
        out = _zero()
        for j in poses.JOINTS:
            a, b = self._from.get(j, 0.0), self._to.get(j, 0.0)
            out[j] = (a + (b - a) * w) * style.amplitude
        return out


class LookAt(Layer):
    """Spring-smoothed tracking offset, in degrees from center.

    `set_target(yaw, pitch)` is what perception calls. The spring keeps the arm
    from snapping to a jittery detection box.
    """
    name = "look_at"

    def __init__(self, stiffness=6.0, damping=1.0):
        self.stiffness = stiffness
        self.damping = damping
        self.target = {"base_yaw": 0.0, "wrist": 0.0}
        self.value = {"base_yaw": 0.0, "wrist": 0.0}
        self.vel = {"base_yaw": 0.0, "wrist": 0.0}
        self.weight = 0.0          # off until something is being tracked
        self._last = None

    def set_target(self, yaw=None, pitch=None):
        if yaw is not None:
            self.target["base_yaw"] = float(yaw)
        if pitch is not None:
            self.target["wrist"] = float(pitch)

    def sample(self, now, style):
        dt = 0.02 if self._last is None else max(1e-4, min(0.1, now - self._last))
        self._last = now
        out = _zero()
        for j in ("base_yaw", "wrist"):
            err = self.target[j] - self.value[j]
            self.vel[j] += (self.stiffness * err
                            - 2.0 * self.damping * math.sqrt(self.stiffness)
                            * self.vel[j]) * dt
            self.value[j] += self.vel[j] * dt
            out[j] = self.value[j]
        return out


class SpeechBob(Layer):
    """A small wrist pitch that follows speech or beep loudness (0..1)."""
    name = "speech_bob"

    def __init__(self, amp=3.0):
        self.amp = amp
        self.level = 0.0
        self.weight = 0.0

    def set_level(self, level):
        self.level = max(0.0, min(1.0, float(level)))

    def sample(self, now, style):
        out = _zero()
        out["wrist"] = -self.level * self.amp * style.amplitude
        return out


# --- style ------------------------------------------------------------------
class Style:
    """Mood's handles on motion (BUILD_SPEC 6.2). Mood owns these, not motion."""

    def __init__(self, speed=1.0, amplitude=1.0, bounce=0.0):
        self.speed = float(speed)
        self.amplitude = float(amplitude)
        self.bounce = float(bounce)

    def ease_for(self, default):
        """Bounce swaps a calm curve for an overshooting one."""
        if self.bounce >= 0.5 and default in ("ease_in_out", "ease_out"):
            return "ease_out_back"
        return default


# --- the engine -------------------------------------------------------------
class MotionEngine:
    def __init__(self, config, backend, start_pose="HOME", style=None,
                 suppress_cb=None, rng=None, drive_undriven=False):
        self.config = config
        self.backend = backend
        self.base = poses.get_pose(start_pose) if isinstance(start_pose, str) \
            else dict(start_pose)
        self.style = style or Style()
        self.limiter = safety.Limiter(config, start=self.base,
                                      drive_undriven=drive_undriven)
        self.suppress_cb = suppress_cb
        self.rng = rng or random.Random()

        self.breathing = Breathing()
        self.saccades = Saccades(rng=self.rng)
        self.look_at = LookAt()
        self.speech_bob = SpeechBob()
        self.layers = [self.breathing, self.saccades, self.look_at,
                       self.speech_bob]

        self.queue = []
        self.primary = None
        self._primary_t0 = 0.0
        self._fading = None          # (move, t0, fade_t0)
        self._last_tick = None
        self.last_sent = dict(self.base)
        self.ticks = 0

    # --- control ---
    def push(self, move, interrupt=True):
        """Queue a move. interrupt=True cross-fades out of whatever is running."""
        if interrupt:
            self.queue = [move]
            if self.primary is not None:
                self._fading = (self.primary, self._primary_t0, None)
            self.primary = None
        else:
            self.queue.append(move)
        return move

    def clear(self):
        self.queue = []
        if self.primary is not None:
            self._fading = (self.primary, self._primary_t0, None)
        self.primary = None

    def busy(self):
        return self.primary is not None or bool(self.queue)

    def at_pose(self, name, tol=1.5):
        want = poses.get_pose(name)
        return all(abs(self.base[j] - want[j]) <= tol for j in poses.JOINTS)

    # --- the tick ---
    def tick(self, now):
        dt = 1.0 / self.config.tick_hz if self._last_tick is None \
            else max(0.0, now - self._last_tick)
        self._last_tick = now

        if self.primary is None and self.queue:
            self.primary = self.queue.pop(0)
            self._primary_t0 = now
            self.primary.start(self.base)
            if self.suppress_cb is not None and not self.primary.continuous:
                # Self-motion must never read as a scene change (BUILD_SPEC 6.6).
                self.suppress_cb(now + self.primary.duration + 0.5)

        base, delta = self.base, _zero()
        if self.primary is not None:
            elapsed = (now - self._primary_t0) * self.style.speed
            b, d = self.primary.sample(elapsed)
            if b is not None:
                base = b
            delta = d
            if self.primary.done(elapsed):
                if b is not None:
                    self.base = dict(b)
                self.primary = None

        # cross-fade: keep sampling the move we interrupted, ramping it out
        if self._fading is not None:
            move, t0, fade_t0 = self._fading
            if fade_t0 is None:
                fade_t0 = now
                self._fading = (move, t0, fade_t0)
            w = min(1.0, (now - fade_t0) / CROSSFADE_S)
            if w >= 1.0:
                self._fading = None
            else:
                fb, fd = move.sample((now - t0) * self.style.speed)
                for j in poses.JOINTS:
                    delta[j] = delta[j] * w + fd[j] * (1.0 - w)
                if fb is not None:
                    base = {j: base[j] * w + fb[j] * (1.0 - w)
                            for j in poses.JOINTS}

        if base is not self.base and self.primary is None and self._fading is None:
            self.base = dict(base)

        requested = {}
        for j in poses.JOINTS:
            v = base[j] + delta[j] * self.style.amplitude
            for layer in self.layers:
                if layer.weight > 0.0:
                    v += layer.sample(now, self.style)[j] * layer.weight
            requested[j] = v

        out = self.limiter.apply(requested, dt)
        self.backend.send(out, now)
        self.last_sent = out
        self.ticks += 1
        return out

    def run(self, seconds, now=0.0, step=None):
        """Drive the engine for `seconds` of simulated time. Tests use this."""
        step = step or 1.0 / self.config.tick_hz
        t_end = now + seconds
        while now < t_end:
            self.tick(now)
            now += step
        return now
