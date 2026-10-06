"""What to look at, and how to keep looking at it. BUILD_SPEC 5.3.

Two jobs, kept apart because they fail differently.

CHOOSING is a priority list. The most salient thing wins outright rather than
being blended with the others, because a gaze that averages two targets looks
at neither:

    1. Shaurya speaking (wake word or voice activity)
    2. motion near him
    3. the curiosity target
    4. Shaurya's face
    5. a random spot (idle gaze)

FOLLOWING is closed-loop visual servoing, the same shape as the AVI tracker:
a correction from the target box's offset from image centre, fed to the
spring-smoothed LookAt layer so it settles rather than snapping.

The gain is in COMMANDED DEGREES PER FRAME WIDTH, measured, and is not the
camera's field of view -- see `config/joints.toml`'s `[camera]`. Using the
datasheet's 52 degrees made the loop apply roughly twice the intended gain and
oscillate instead of settling.
"""
import random
import time

# Priority order. Lower number wins.
SOURCES = ("speech", "motion", "curiosity", "face", "idle")
PRIORITY = {name: i for i, name in enumerate(SOURCES)}

# How long a target stays worth looking at after its last update, so a single
# dropped detection does not throw the gaze away.
STICKY_S = {"speech": 2.5, "motion": 1.5, "curiosity": 60.0,
            "face": 2.0, "idle": 4.0}

# Fraction of the measured error corrected per update. Below 1.0 on purpose:
# the loop runs faster than the arm, so asking for the whole error every time
# commands a move that is still in progress when the next correction is
# computed, which is how a tracker ends up hunting.
GAIN = 0.45

# Inside this, the target is close enough. BUILD_SPEC 15's M3 test is "keeps a
# person box within +-5 deg of centre", so the deadband has to be well under
# that or the loop will sit just outside it.
DEADBAND_FRAC = 0.03

IDLE_YAW_SPREAD = 0.22      # how far off centre an idle glance may wander


class Target:
    def __init__(self, source, cx, cy, width, height, label=None, conf=1.0,
                 at=0.0):
        self.source = source
        self.cx = float(cx)
        self.cy = float(cy)
        self.width = float(width)
        self.height = float(height)
        self.label = label
        self.conf = float(conf)
        self.at = float(at)

    @property
    def offset(self):
        """(x, y) offset from image centre, as a fraction of frame size."""
        return ((self.cx - self.width / 2.0) / self.width,
                (self.cy - self.height / 2.0) / self.height)

    def __repr__(self):
        ox, oy = self.offset
        return (f"<Target {self.source} {self.label or ''} "
                f"offset {ox:+.3f},{oy:+.3f}>")


class Attention:
    def __init__(self, config, clock=time.time, rng=None, gain=GAIN):
        self.config = config
        self.clock = clock
        self.rng = rng or random.Random()
        self.gain = float(gain)
        self.targets = {}
        self.current = None
        self._idle_until = 0.0

    # --- choosing --------------------------------------------------------
    def offer(self, source, cx, cy, width, height, label=None, conf=1.0):
        """Tell attention that something is visible. Does not choose."""
        if source not in PRIORITY:
            raise ValueError(f"unknown attention source {source!r}; "
                             f"have {list(SOURCES)}")
        self.targets[source] = Target(source, cx, cy, width, height,
                                      label=label, conf=conf, at=self.clock())

    def _fresh(self, now):
        return {s: t for s, t in self.targets.items()
                if now - t.at <= STICKY_S[s]}

    def choose(self, now=None, frame=(1280, 720)):
        """Return the Target to look at, or None if there is nothing.

        Idle gaze is generated here rather than offered from outside, because
        nothing else knows when the robot has run out of things to look at.
        """
        now = self.clock() if now is None else now
        fresh = self._fresh(now)
        for source in SOURCES:
            if source == "idle":
                break
            if source in fresh:
                self.current = fresh[source]
                return self.current
        # Nothing salient. Wander, but not continuously: a new random spot
        # every few seconds reads as looking around, while a new one every
        # update reads as a fault.
        if "idle" not in fresh or now >= self._idle_until:
            w, h = frame
            self.targets["idle"] = Target(
                "idle",
                cx=w / 2.0 * (1.0 + self.rng.uniform(-IDLE_YAW_SPREAD,
                                                     IDLE_YAW_SPREAD) * 2),
                cy=h / 2.0 * (1.0 + self.rng.uniform(-0.10, 0.10) * 2),
                width=w, height=h, label="idle gaze", at=now)
            self._idle_until = now + self.rng.uniform(2.0, 5.0)
        self.current = self.targets["idle"]
        return self.current

    def drop(self, source):
        self.targets.pop(source, None)
        if self.current is not None and self.current.source == source:
            self.current = None

    # --- following -------------------------------------------------------
    def corrections(self, target=None):
        """Degrees to add to yaw and wrist to bring `target` toward centre.

        Returns (d_yaw, d_wrist). Both are zero inside the deadband, so a
        centred target produces no command at all rather than a stream of
        sub-degree jitter the arm cannot act on anyway.
        """
        target = target or self.current
        if target is None:
            return 0.0, 0.0
        ox, oy = target.offset
        cam = self.config.camera
        d_yaw = 0.0
        d_wrist = 0.0
        if abs(ox) > DEADBAND_FRAC:
            # Negative because the correction opposes the offset: a target to
            # the right of centre needs the view to move right, which brings
            # the target back toward the middle.
            d_yaw = -ox * float(cam["yaw_deg_per_frame"]) * self.gain
        if abs(oy) > DEADBAND_FRAC:
            # Higher wrist degrees tilt the view UP (measured; see
            # config/poses.toml), and a target BELOW centre has oy > 0, so it
            # needs the view to go down: also negative.
            d_wrist = -oy * float(cam["wrist_deg_per_frame"]) * self.gain
        return d_yaw, d_wrist

    def error_degrees(self, target=None):
        """How far off centre the target is, in degrees, for the M3 test.

        BUILD_SPEC 15 asks that look-at keep a person box within +-5 deg of
        centre, so the acceptance criterion needs the error in degrees rather
        than in frame fractions.
        """
        target = target or self.current
        if target is None:
            return 0.0, 0.0
        ox, oy = target.offset
        cam = self.config.camera
        return (ox * float(cam["yaw_deg_per_frame"]),
                oy * float(cam["wrist_deg_per_frame"]))

    def steer(self, look_at_layer, target=None, weight=1.0):
        """Push the correction into the engine's spring-smoothed LookAt layer.

        The spring is what stops it snapping; this only ever moves the spring's
        target, never the joint, so a jittery detection cannot become a jittery
        arm.
        """
        d_yaw, d_wrist = self.corrections(target)
        look_at_layer.set_target(
            yaw=look_at_layer.target["base_yaw"] + d_yaw,
            pitch=look_at_layer.target["wrist"] + d_wrist)
        look_at_layer.weight = weight if (d_yaw or d_wrist) else \
            look_at_layer.weight
        return d_yaw, d_wrist
