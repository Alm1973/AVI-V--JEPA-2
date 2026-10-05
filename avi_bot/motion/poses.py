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
        return float(self.meta["speed_cap_deg_s"])

    def soft_limits(self, joint):
        j = self.joints[joint]
        return float(j["soft_min"]), float(j["soft_max"])

    def is_driven(self, joint):
        return bool(self.joints[joint]["driven"])

    def sign(self, joint):
        return int(self.joints[joint]["sign"])

    def deg_to_count(self, joint, deg):
        """The piecewise map documented at the top of config/joints.toml.

        Kept here as well as in the firmware so the sim and the dashboard can
        show real counts, and so a test can prove the two agree.
        """
        j = self.joints[joint]
        lo, mid, hi = j["min_count"], j["center_count"], j["max_count"]
        deg = max(0.0, min(180.0, float(deg)))
        if deg <= 90.0:
            return lo + (mid - lo) * (deg / 90.0)
        return mid + (hi - mid) * ((deg - 90.0) / 90.0)


def load(path=None):
    with open(path or CONFIG_PATH, "rb") as f:
        return JointConfig(tomllib.load(f))


def pose(base_yaw=90.0, shoulder=90.0, elbow=90.0, wrist=90.0):
    return {"base_yaw": base_yaw, "shoulder": shoulder, "elbow": elbow,
            "wrist": wrist}


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

# OBSERVE is the one pose the observer is allowed to log object positions from
# (BUILD_SPEC 6.1 and 6.6), so it must stay pinned to the calibrated center.
assert POSES["OBSERVE"] == pose(), "OBSERVE must be the calibrated center pose"


def get_pose(name):
    try:
        return dict(POSES[name])
    except KeyError:
        raise KeyError(f"unknown pose {name!r}; have {sorted(POSES)}")
