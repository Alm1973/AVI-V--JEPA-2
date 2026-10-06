"""Joint config and the named poses.

Angles are degrees, 0..180, with 90 the bench-calibrated center. JOINTS is the
canonical order and it is the order the `P` wire protocol uses, so it must match
the PCA9685 channel numbers.
"""
import tomllib
from pathlib import Path

JOINTS = ("base_yaw", "shoulder", "elbow", "wrist")

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "joints.toml"


class JointConfig:
    def __init__(self, data):
        self.meta = data["meta"]
        self.joints = data["joints"]
        self.sim = data.get("sim", {})
        channels = [self.joints[j]["channel"] for j in JOINTS]
        if channels != [0, 1, 2, 3]:
            raise ValueError(
                f"JOINTS order must match PCA9685 channels 0-3, got {channels}. "
                "The wire protocol sends angles positionally, so a mismatch here "
                "drives the wrong servo.")

    @property
    def tick_hz(self):
        return float(self.meta["tick_hz"])

    @property
    def speed_cap(self):
        """The safety clamp: the fastest anything may be COMMANDED."""
        return float(self.meta["speed_cap_deg_s"])

    @property
    def achievable(self):
        """What the arm can actually do. Gesture durations derive from this."""
        return float(self.meta.get("achievable_deg_s", self.speed_cap))

    def soft_limits(self, joint):
        j = self.joints[joint]
        return float(j["soft_min"]), float(j["soft_max"])

    def is_driven(self, joint):
        return bool(self.joints[joint]["driven"])

    def sign(self, joint):
        return int(self.joints[joint]["sign"])

    def apply_sign(self, joint, deg):
        """Reflect a logical angle about center when the joint runs backwards.

        Logical degrees are what the whole system reasons in: for base_yaw,
        larger means "look further right". Which direction that is in PWM
        counts is a wiring and linkage fact, so it lives here and nowhere else.
        Getting it wrong does more than reverse a key: LookAt is a feedback
        loop, and an inverted sign makes it accelerate away from its target
        instead of settling on it.
        """
        s = self.sign(joint)
        if s >= 0:
            return deg
        return 180.0 - deg

    def deg_to_count(self, joint, deg):
        """The piecewise map documented at the top of config/joints.toml.

        Kept here as well as in the firmware so the sim and the dashboard can
        show real counts, and so a test can prove the two agree.
        """
        j = self.joints[joint]
        lo, mid, hi = j["min_count"], j["center_count"], j["max_count"]
        deg = max(0.0, min(180.0, self.apply_sign(joint, float(deg))))
        if deg <= 90.0:
            return lo + (mid - lo) * (deg / 90.0)
        return mid + (hi - mid) * ((deg - 90.0) / 90.0)


def load(path=None):
    with open(path or CONFIG_PATH, "rb") as f:
        return JointConfig(tomllib.load(f))


def pose(base_yaw=90.0, shoulder=90.0, elbow=90.0, wrist=90.0):
    return {"base_yaw": base_yaw, "shoulder": shoulder, "elbow": elbow,
            "wrist": wrist}


# Named poses. The real ones are TAUGHT ON THE ARM and live in
# config/poses.toml; the table below is only a fallback for when that file is
# missing, so the sim and the tests still run.
#
# The fallback values are deliberately timid. An earlier hand-written table
# claimed TALL was above centre and LOW below, and both in fact leaned the arm
# down, because the shoulder's two ends are both down with upright in the
# middle. Guessing that geometry is what taught poses exist to stop.
#
# Directions, from the taught data rather than from inference:
#   LOWER shoulder degrees RAISE the arm   (ATTENTIVE 29.5, REST 150)
#   HIGHER wrist degrees TILT THE CAMERA UP (REST, camera down, is 30.5)
FALLBACK_POSES = {
    "HOME":      pose(),
    "OBSERVE":   pose(),
    "ATTENTIVE": pose(shoulder=80.0),
    "TALL":      pose(shoulder=75.0),
    "LOW":       pose(shoulder=110.0),
    "REST":      pose(shoulder=125.0, wrist=60.0),
}

POSES_PATH = Path(__file__).resolve().parents[2] / "config" / "poses.toml"


def _load_poses(path=None):
    path = Path(path) if path else POSES_PATH
    if not path.exists():
        return dict(FALLBACK_POSES), False, set()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    out = {}
    meta = data.pop("meta", {}) or {}
    for name, body in data.items():
        if not isinstance(body, dict):
            continue
        missing = [j for j in JOINTS if j not in body]
        if missing:
            raise ValueError(f"pose {name} in {path.name} is missing "
                             f"{missing}; a partly-specified pose would put "
                             f"the arm somewhere nobody chose")
        out[name] = {j: float(body[j]) for j in JOINTS}
    for name in FALLBACK_POSES:
        out.setdefault(name, dict(FALLBACK_POSES[name]))
    return out, True, set(meta.get("known_duplicates", []))


POSES, POSES_ARE_TAUGHT, KNOWN_DUPLICATE_POSES = _load_poses()

# OBSERVE is the one pose the observer may log object positions from
# (BUILD_SPEC 6.1 and 6.6), so it has to be a FIXED, fully-specified pose --
# otherwise every "left side of the desk" in the database means something
# different. It is NOT the mechanical centre: measured 2026-10-05, the camera
# at wrist centre points at the carpet, so a centred OBSERVE would have the
# logger faithfully recording the floor.
assert set(POSES["OBSERVE"]) == set(JOINTS), "OBSERVE must name every joint"


def get_pose(name):
    try:
        return dict(POSES[name])
    except KeyError:
        raise KeyError(f"unknown pose {name!r}; have {sorted(POSES)}")
