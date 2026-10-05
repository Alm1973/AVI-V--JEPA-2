"""The firmware and config/joints.toml must not drift apart.

Both hold the same calibration table, on purpose: the firmware keeps its own
copy so it stays safe even if the host sends nonsense. Two copies means they
can disagree, and a disagreement would be invisible -- the arm would simply go
to slightly the wrong place, or clamp somewhere the host did not expect. So
this test parses the sketch and compares, rather than trusting a comment that
says they match.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from avi_bot.motion import poses

SKETCH = ROOT / "firmware" / "avi_servo_v2" / "avi_servo_v2.ino"

ROW = re.compile(
    r"\{\s*(\d+),\s*(\d+),\s*(\d+),\s*"        # min, center, max
    r"([\d.]+)f,\s*([\d.]+)f,\s*"              # softMin, softMax
    r"(true|false)\s*\}")


def parse_sketch():
    text = SKETCH.read_text()
    body = text.split("JOINTS[NJ] = {", 1)[1].split("};", 1)[0]
    rows = ROW.findall(body)
    out = []
    for mn, ct, mx, lo, hi, driven in rows:
        out.append({"min_count": int(mn), "center_count": int(ct),
                    "max_count": int(mx), "soft_min": float(lo),
                    "soft_max": float(hi), "driven": driven == "true"})
    return out, text


def firmware_deg_to_count(row, deg):
    """The sketch's degToCount, including its +0.5 integer rounding."""
    deg = max(0.0, min(180.0, deg))
    if deg <= 90.0:
        c = row["min_count"] + (row["center_count"] - row["min_count"]) * (deg / 90.0)
    else:
        c = row["center_count"] + (row["max_count"] - row["center_count"]) * ((deg - 90.0) / 90.0)
    return int(c + 0.5)


class TestFirmwareAgreesWithConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.text = parse_sketch()
        cls.config = poses.load()

    def test_sketch_has_one_row_per_joint_in_channel_order(self):
        self.assertEqual(len(self.rows), len(poses.JOINTS))

    def test_calibration_and_limits_match(self):
        for i, joint in enumerate(poses.JOINTS):
            row = self.rows[i]
            cfg = self.config.joints[joint]
            for key in ("min_count", "center_count", "max_count",
                        "soft_min", "soft_max"):
                self.assertEqual(row[key], cfg[key],
                                 f"{joint}.{key}: sketch {row[key]} != "
                                 f"joints.toml {cfg[key]}")
            self.assertEqual(row["driven"], bool(cfg["driven"]), joint)

    def test_shoulder_bug_from_v1_is_actually_fixed(self):
        # v1 drove channel 1 with the wrist's range. If that ever comes back,
        # the shoulder gets a wrong zero and every pose is off.
        shoulder = self.rows[1]
        wrist = self.rows[3]
        self.assertEqual(shoulder["min_count"], 75)
        self.assertEqual(shoulder["center_count"], 370)
        self.assertNotEqual(
            (shoulder["min_count"], shoulder["center_count"]),
            (wrist["min_count"], wrist["center_count"]),
            "channel 1 is using the wrist's range again")

    def test_the_two_degree_to_count_maps_agree_everywhere(self):
        for i, joint in enumerate(poses.JOINTS):
            for tenth in range(0, 1801):
                deg = tenth / 10.0
                host = round(self.config.deg_to_count(joint, deg))
                fw = firmware_deg_to_count(self.rows[i], deg)
                self.assertLessEqual(
                    abs(host - fw), 1,
                    f"{joint} at {deg} deg: host {host} vs firmware {fw}")

    def test_speed_cap_and_watchdog_match_the_config(self):
        cap = float(re.search(r"SPEED_CAP_DEG_S\s*=\s*([\d.]+)f", self.text).group(1))
        dog = int(re.search(r"WATCHDOG_MS\s*=\s*(\d+)", self.text).group(1))
        self.assertEqual(cap, self.config.speed_cap)
        self.assertEqual(dog, int(self.config.meta["watchdog_ms"]))

    def test_no_servo_position_is_written_in_setup(self):
        # The whole point of v2's startup. A setPWM(channel, ...) in setup() is
        # the jump. setPWMFreq is a different call and is guarded separately,
        # so strip comments and match the position write precisely.
        setup = self.text.split("void setup()", 1)[1].split("void loop()", 1)[0]
        setup = re.sub(r"//[^\n]*", "", setup)
        self.assertIsNone(
            re.search(r"\bsetPWM\s*\(", setup),
            "setup() writes a servo position, which is the startup jump this "
            "firmware exists to prevent")

    def test_setup_does_not_blindly_reprogram_the_pwm_frequency(self):
        # Loading the prescaler requires sleeping the chip, which cuts the
        # pulses and lets a loaded arm sag. It must be conditional.
        setup = re.sub(r"//[^\n]*", "",
                       self.text.split("void setup()", 1)[1]
                                .split("void loop()", 1)[0])
        self.assertIsNone(re.search(r"\bsetPWMFreq\s*\(", setup),
                          "setup() calls setPWMFreq unconditionally")
        self.assertIn("ensurePwmFreq()", setup)
        guard = self.text.split("static bool ensurePwmFreq()", 1)[1]
        self.assertIsNotNone(
            re.search(r"if \(have == want && !asleep\)", guard),
            "ensurePwmFreq no longer short-circuits when the chip is already "
            "at the right frequency, so every boot will sleep the outputs")

    def test_the_arm_position_is_read_before_anything_is_written(self):
        """The ordering bug that made the read-back useless on the first build.

        Reading the PCA9685's output registers only tells you where the arm is
        while nothing has overwritten them yet. The first flashed build called
        Adafruit's pwm.begin() first, which resets the chip and sets the PWM
        frequency to 1 kHz, so the read always came back UNKNOWN. Verified on
        hardware 2026-10-05.
        """
        setup = re.sub(r"//[^\n]*", "",
                       self.text.split("void setup()", 1)[1]
                                .split("void loop()", 1)[0])
        read_at = setup.find("setup_start_pose()")
        freq_at = setup.find("ensurePwmFreq()")
        self.assertNotEqual(read_at, -1, "setup() never reads the start pose")
        self.assertNotEqual(freq_at, -1)
        self.assertLess(read_at, freq_at,
                        "setup() touches the PWM registers before reading the "
                        "arm's position back, which makes the read meaningless")

    def test_readback_only_requires_the_driven_channels(self):
        """Undriven channels have never been written, so they read 0, which is
        outside their calibrated range. Requiring them made the read-back
        impossible on every boot."""
        body = self.text.split("static void setup_start_pose()", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (!JOINTS[j].driven)", body,
                      "setup_start_pose demands a read-back from channels that "
                      "nothing ever writes, so it can never succeed")
        # and the skip must come before the read, not after it
        self.assertLess(body.find("if (!JOINTS[j].driven)"),
                        body.find("readOffCount(j)"))

    def test_the_adafruit_driver_is_not_used(self):
        # Its begin() resets the chip and reprograms the frequency, which is
        # precisely the state this firmware needs to preserve.
        code = re.sub(r"//[^\n]*", "", self.text)
        self.assertNotIn("Adafruit_PWMServoDriver", code)
        self.assertIsNone(re.search(r"\bpwm\.", code))

    def test_the_loop_refuses_to_move_before_the_first_command(self):
        loop = self.text.split("void loop()", 1)[1]
        self.assertIn("if (!haveTarget) return;", loop)
        self.assertIn("if (holding) return;", loop)
        self.assertIn("> WATCHDOG_MS) return;", loop)


if __name__ == "__main__":
    unittest.main(verbosity=2)
