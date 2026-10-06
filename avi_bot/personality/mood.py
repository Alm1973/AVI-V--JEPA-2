"""Mood: a few numbers that drift back to baseline, and the label they imply.

BUILD_SPEC 5.2. Events nudge the numbers, time pulls them home, and an emotion
label falls out of wherever they currently sit. Nothing here knows about
servos, audio or the clock beyond what it is handed, so it is testable at any
speed and a test does not have to wait fifteen real minutes for a half-life.

Decay is a half-life rather than a fixed step per tick, which means the result
does not depend on how often tick() happens to be called. A fixed step would
make mood drift faster on a busy loop than a quiet one.
"""
import math
import time

# BUILD_SPEC 5.2's table. Half-lives in seconds.
BASELINE = {"valence": 0.2, "arousal": 0.3, "social": 0.2}
HALF_LIFE = {"valence": 15 * 60.0, "arousal": 3 * 60.0}
LIMITS = {"valence": (-1.0, 1.0), "arousal": (0.0, 1.0),
          "social": (0.0, 1.0), "energy": (0.0, 1.0)}

SULK_S = 5 * 60.0          # a scolding lasts this long
CURIOSITY_S = 60.0         # how long something new stays interesting
IGNORED_STEP_S = 15 * 60.0  # social rises this often while he is present
IGNORED_AFTER_S = 45 * 60.0  # ... once there has been no interaction this long

# Quiet hours, from BUILD_SPEC's speaking-up gates. Energy bottoms out here.
QUIET_FROM, QUIET_TO = 22, 7


def _clamp(name, v):
    lo, hi = LIMITS[name]
    return max(lo, min(hi, v))


def energy_for_hour(hour, minute=0):
    """Energy from the time of day: lowest in quiet hours, peak mid-afternoon.

    A smooth curve rather than a step, so a behaviour does not flip the instant
    a clock minute rolls over. 15:00 is the peak because that is when his desk
    hours start; 03:00 is the trough.
    """
    t = (hour + minute / 60.0) % 24.0
    if QUIET_FROM <= t or t < QUIET_TO:
        return 0.15
    # 07:00 -> 22:00 mapped onto half a cosine, peaking at about 14:30.
    frac = (t - QUIET_TO) / float(QUIET_FROM - QUIET_TO)
    return 0.35 + 0.6 * math.sin(math.pi * frac)


class Mood:
    def __init__(self, clock=time.time, hour_fn=None):
        self.clock = clock
        # Injected so a test can place the robot at 3 a.m. without touching the
        # system clock.
        self.hour_fn = hour_fn or (lambda: time.localtime(self.clock())[3:5])
        now = self.clock()
        self.valence = BASELINE["valence"]
        self.arousal = BASELINE["arousal"]
        self.social = BASELINE["social"]
        self.sulk_until = 0.0
        self.curiosity_target = None
        self.curiosity_until = 0.0
        self.present = False
        self.last_interaction = now
        self.last_social_bump = now
        self.last_tick = now
        self.focus = False
        self.log = []

    # --- events ----------------------------------------------------------
    def event(self, name, target=None):
        """Apply one of BUILD_SPEC 5.2's events. Unknown names raise."""
        now = self.clock()
        if name == "praise":
            self.valence = _clamp("valence", self.valence + 0.4)
            self.arousal = _clamp("arousal", self.arousal + 0.3)
            self.sulk_until = 0.0          # praise ends a sulk outright
            self.last_interaction = now
        elif name == "scold":
            self.valence = _clamp("valence", self.valence - 0.5)
            self.sulk_until = now + SULK_S
            self.last_interaction = now
        elif name == "arrived":
            self.valence = _clamp("valence", self.valence + 0.2)
            self.arousal = _clamp("arousal", self.arousal + 0.4)
            self.social = 0.0
            self.present = True
            self.last_interaction = now
            self.last_social_bump = now
        elif name == "left":
            self.arousal = _clamp("arousal", self.arousal - 0.2)
            self.present = False
        elif name == "task_ok":
            self.valence = _clamp("valence", self.valence + 0.2)
            self.last_interaction = now
        elif name == "task_failed":
            self.valence = _clamp("valence", self.valence - 0.1)
            self.last_interaction = now
        elif name == "new_object":
            self.arousal = _clamp("arousal", self.arousal + 0.2)
            self.curiosity_target = target
            self.curiosity_until = now + CURIOSITY_S
        elif name == "focus_start":
            self.arousal = min(self.arousal, 0.15)
            self.focus = True
            self.last_interaction = now
        elif name == "focus_end":
            self.focus = False
            self.last_interaction = now
        else:
            raise ValueError(f"unknown mood event {name!r}")
        self.log.append((now, name))
        del self.log[:-50]

    # --- time ------------------------------------------------------------
    def tick(self, now=None):
        now = self.clock() if now is None else now
        dt = max(0.0, now - self.last_tick)
        self.last_tick = now
        if dt == 0.0:
            return

        for name in ("valence", "arousal"):
            base = BASELINE[name]
            decay = 0.5 ** (dt / HALF_LIFE[name])
            setattr(self, name,
                    _clamp(name, base + (getattr(self, name) - base) * decay))

        # Social does not decay: being ignored is cumulative. It only resets
        # when he turns up or interacts.
        if (self.present
                and now - self.last_interaction >= IGNORED_AFTER_S
                and now - self.last_social_bump >= IGNORED_STEP_S):
            self.social = _clamp("social", self.social + 0.1)
            self.last_social_bump = now

        if self.curiosity_target is not None and now >= self.curiosity_until:
            self.curiosity_target = None

    @property
    def energy(self):
        hour, minute = self.hour_fn()
        return energy_for_hour(hour, minute)

    def sulking(self, now=None):
        now = self.clock() if now is None else now
        return now < self.sulk_until

    # --- the label -------------------------------------------------------
    def emotion(self, now=None):
        """One word for how it feels, from BUILD_SPEC 5.2's rules.

        ONE DELIBERATE DEPARTURE FROM THE SPEC'S ORDER: sulky is tested before
        curious and bored, not after. The spec lists curious first, which would
        mean a new object on the desk cancels a sulk the instant it appears --
        and a sulk you can distract out of in one second is not a sulk. The
        character sheet asks for "a short sulk after a scolding", so it is
        given its five minutes. Praise still clears it immediately, which is
        the intended way out.
        """
        now = self.clock() if now is None else now
        if self.sulking(now):
            return "sulky"
        if self.energy < 0.3:
            return "sleepy"
        if self.valence > 0.3 and self.arousal > 0.6:
            return "excited"
        if self.valence > 0.3:
            return "happy"
        if self.valence < -0.2:
            return "sad"
        if self.curiosity_target is not None:
            return "curious"
        if self.social > 0.6 and self.arousal < 0.3:
            return "bored"
        if self.valence > 0 and self.arousal < 0.3:
            return "content"
        return "content"

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        return {
            "valence": round(self.valence, 3),
            "arousal": round(self.arousal, 3),
            "social": round(self.social, 3),
            "energy": round(self.energy, 3),
            "emotion": self.emotion(now),
            "sulking": self.sulking(now),
            "curiosity_target": self.curiosity_target,
            "present": self.present,
            "focus": self.focus,
        }


# --- how mood changes the way the arm moves ----------------------------------
# BUILD_SPEC 5.5: the same move should read differently depending on mood.
# speed and amplitude scale the motion; bounce swaps a calm easing curve for an
# overshooting one.
#
# Speeds above 1.0 are deliberately modest. The arm manages about 27 deg/s and
# gestures are already sized to fit that, so asking for 1.5x would not be
# refused -- it would be silently clamped, which smears the gesture into a
# different shape. 1.15 is roughly what the headroom actually allows.
STYLES = {
    "excited":  {"speed": 1.15, "amplitude": 1.10, "bounce": 1.0},
    "happy":    {"speed": 1.10, "amplitude": 1.05, "bounce": 1.0},
    "content":  {"speed": 1.00, "amplitude": 1.00, "bounce": 0.0},
    "curious":  {"speed": 1.00, "amplitude": 1.00, "bounce": 0.0},
    "bored":    {"speed": 0.85, "amplitude": 0.90, "bounce": 0.0},
    "sulky":    {"speed": 0.70, "amplitude": 0.70, "bounce": 0.0},
    "sad":      {"speed": 0.70, "amplitude": 0.80, "bounce": 0.0},
    "sleepy":   {"speed": 0.60, "amplitude": 0.75, "bounce": 0.0},
}

# Sulking is "reluctant and delayed" (BUILD_SPEC 5.5), and delay is not
# something a Style can express, so it is carried separately for whoever starts
# the move.
START_DELAY_S = {"sulky": 0.6, "sad": 0.3, "sleepy": 0.4}


def style_for(emotion):
    """Returns the kwargs for avi_bot.motion.engine.Style."""
    return dict(STYLES.get(emotion, STYLES["content"]))


def start_delay_for(emotion):
    return START_DELAY_S.get(emotion, 0.0)
