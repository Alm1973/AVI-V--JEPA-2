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


# WRONG GEOMETRY, KNOWN, NOT YET FIXED -- read this before trusting a pose.
#
# Shaurya's calibration (2026-10-05) says the shoulder's two ends are BOTH
# down: count 75 is front/down, 580 is back/down, and ~370, the centre, is
# upright. So the arm is at its tallest at centre and every shoulder movement
# lowers it. There is no "taller than centre" to put TALL at.
#
# The poses below were written assuming a simple up/down axis, so TALL (below
# centre) and LOW (above centre) are in fact both leaning the arm down, just in
# opposite directions. They are safe -- well inside the soft limits -- but they
# do not mean what their names say, and perk_up/droop/doze inherit that.
#
# Fixing it properly needs two things measured on the real arm: which shoulder
# direction aims the camera usefully at the desk, and where the camera actually
# points at centre (measured: at the carpet, which is why the wrist is now
# driven). Left as-is deliberately rather than guessed at.
#
# Starting points only. BUILD_SPEC says to tune these on hardware, and they are
# all well inside the soft limits so that tuning cannot walk into an end-stop.
#
# Sign conventions, both confirmed on hardware:
#   base_yaw + -> view moves RIGHT
#   shoulder + -> view moves DOWN
# The elbow and wrist signs are UNKNOWN (joints.toml sign = 0), so no pose here
# moves them away from center yet. Once M1 measures them, LOW/TALL/REST get
# their elbow and wrist values.
POSES = {
    "HOME":      pose(),
    "OBSERVE":   pose(),                       # identical to HOME by definition
    "ATTENTIVE": pose(shoulder=80.0),          # slightly raised, camera up a bit
    "TALL":      pose(shoulder=68.0),          # alert
    "LOW":       pose(shoulder=112.0),         # shy / sulky
    "REST":      pose(shoulder=125.0),         # folded, camera down
}

# OBSERVE is the one pose the observer may log object positions from
# (BUILD_SPEC 6.1 and 6.6), so it has to be a FIXED, recorded pose -- otherwise
# every "left side of the desk" in the database means something different.
#
# It is deliberately NOT the mechanical centre any more. Measured 2026-10-05:
# the camera at wrist centre points at the carpet, so a centred OBSERVE would
# have the logger faithfully recording the floor. OBSERVE_WRIST below is the
# aim, and it still needs setting from the real desk -- see docs/HANDOFF.md.
assert set(POSES["OBSERVE"]) == set(JOINTS), "OBSERVE must name every joint"
assert POSES["OBSERVE"]["base_yaw"] == 90.0, "OBSERVE must be centred in yaw"


def get_pose(name):
    try:
        return dict(POSES[name])
    except KeyError:
        raise KeyError(f"unknown pose {name!r}; have {sorted(POSES)}")
