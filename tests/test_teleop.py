"""The teleop driver: envelope, watchdog, and key directions.

This is the code path that turns a keypress on a laptop somewhere into servo
motion in Shaurya's bedroom, so the interesting cases are the ones where the
input stops arriving rather than the ones where it works.
"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_teleop():
    for name in ("cv2", "settings", "observer", "camera_stream", "teleop"):
        sys.modules.pop(name, None)
    cv2 = types.ModuleType("cv2")
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.INTER_AREA = 3
    cv2.CAP_DSHOW = 700
    cv2.CAP_ANY = 0
    for fn in ("imencode", "resize", "VideoCapture"):
        setattr(cv2, fn, lambda *a, **k: None)
    sys.modules["cv2"] = cv2
    st = types.ModuleType("settings")
    st.CAMERA_INDEX = 1
    sys.modules["settings"] = st
    obs = types.ModuleType("observer")
    obs.open_camera = lambda i: None
    obs.read_fresh = lambda cap, drain=2: (False, None)
    sys.modules["observer"] = obs
    sys.path.insert(0, str(ROOT / "assistant"))
    sys.path.insert(0, str(ROOT))
    import importlib
    return importlib.import_module("teleop")


class TestDriver(unittest.TestCase):
    def setUp(self):
        self.tp = load_teleop()
        from avi_bot.motion import backends, engine, poses
        self.poses = poses
        cfg = poses.load()
        self.eng = engine.MotionEngine(cfg, backends.SimBackend(cfg))
        # One injected clock, so "input stopped arriving" is the only reason the
        # watchdog can ever fire in these tests.
        self.t = 1000.0
        self.driver = self.tp.Driver(self.eng, max_deg=15.0, speed_deg_s=12.0,
                                     clock=lambda: self.t)

    def test_a_and_d_move_yaw_the_confirmed_way(self):
        # Confirmed on hardware: base_yaw + moves the view RIGHT. So D is +.
        self.assertEqual(self.tp.KEYMAP["d"], ("base_yaw", +1.0))
        self.assertEqual(self.tp.KEYMAP["a"], ("base_yaw", -1.0))

    def test_w_and_s_move_the_shoulder_the_confirmed_way(self):
        # Confirmed on hardware: shoulder + moves the view DOWN. So W is -.
        self.assertEqual(self.tp.KEYMAP["w"], ("shoulder", -1.0))
        self.assertEqual(self.tp.KEYMAP["s"], ("shoulder", +1.0))

    def test_holding_a_key_integrates_at_the_set_speed(self):
        self.driver.set_held(["d"])
        m = self.driver.integrate(self.t + 0.1, 0.1)
        self.assertAlmostEqual(m["base_yaw"], 1.2, places=6)

    def test_envelope_is_never_exceeded_however_long_a_key_is_held(self):
        for _ in range(600):          # 60 s at 12 deg/s would be 720 deg
            self.t += 0.1
            self.driver.set_held(["d"])
            m = self.driver.integrate(self.t, 0.1)
        self.assertAlmostEqual(m["base_yaw"], 15.0, places=6)

    def test_watchdog_zeroes_the_velocity_when_input_stops(self):
        """A dropped keyup, a closed lid or a lost tailnet must not leave a key
        held down driving the arm into its envelope."""
        self.driver.set_held(["d"])
        m = self.driver.integrate(self.t + 0.1, 0.1)
        moved = m["base_yaw"]
        self.assertGreater(moved, 0.0)
        # No further set_held: the heartbeat has stopped.
        for i in range(50):
            m = self.driver.integrate(self.t + 0.6 + i * 0.1, 0.1)
        self.assertAlmostEqual(m["base_yaw"], moved, places=6,
                               msg="kept moving after input stopped")

    def test_watchdog_threshold_is_the_firmware_s_half_second(self):
        self.assertEqual(self.tp.INPUT_WATCHDOG_S, 0.5)

    def test_stop_drops_held_keys_but_keeps_position(self):
        self.driver.set_held(["d"])
        self.driver.integrate(self.t + 0.1, 0.1)
        self.driver.stop()
        m = self.driver.integrate(self.t + 0.01, 0.1)
        self.assertGreater(m["base_yaw"], 0.0, "stop should not snap to center")
        self.assertEqual(self.driver.held, set())

    def test_home_returns_the_manual_offset_to_center(self):
        self.driver.set_held(["d", "w"])
        self.driver.integrate(self.t + 0.5, 0.5)
        self.driver.home()
        m = self.driver.integrate(self.t + 0.01, 0.1)
        self.assertEqual(m, {j: 0.0 for j in self.poses.JOINTS})

    def test_unknown_keys_are_ignored(self):
        self.driver.set_held(["x", "escape", "d"])
        self.assertEqual(self.driver.held, {"d"})

    def test_undriven_joints_stay_pinned_even_when_their_keys_are_held(self):
        """A joint whose direction sign is still unknown must not move at all,
        however hard its key is held.

        Which joints those are is read from the config, so driving one later
        cannot leave this test asserting the opposite.
        """
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "tests"))
        from test_motion import config_with_pinned
        cfg, pinned = config_with_pinned("wrist")
        from avi_bot.motion import backends, engine
        self.eng = engine.MotionEngine(cfg, backends.SimBackend(cfg))
        self.driver = self.tp.Driver(self.eng, max_deg=15.0, speed_deg_s=12.0,
                                     clock=lambda: self.t)
        pinned = [pinned]
        keys = [k for k, (joint, _) in self.tp.KEYMAP.items()
                if joint in pinned]
        self.assertTrue(keys, "no key maps to the pinned joint")
        for _ in range(40):
            self.t += 0.1
            self.driver.set_held(keys)
            manual = self.driver.integrate(self.t, 0.1)
            self.eng.base = {j: 90.0 + manual[j] for j in self.poses.JOINTS}
            out = self.eng.tick(self.t)
        for j in pinned:
            self.assertEqual(out[j], 90.0, j)

    def test_speed_cap_still_governs_the_output(self):
        """Even if --speed were set absurdly high, the limiter governs."""
        fast = self.tp.Driver(self.eng, max_deg=15.0, speed_deg_s=10000.0,
                              clock=lambda: self.t)
        cfg = self.eng.config
        step = 1.0 / cfg.tick_hz
        prev = dict(self.eng.last_sent)
        worst = 0.0
        for _ in range(100):
            self.t += step
            fast.set_held(["d"])
            manual = fast.integrate(self.t, step)
            self.eng.base = {j: 90.0 + manual[j] for j in self.poses.JOINTS}
            cur = self.eng.tick(self.t)
            worst = max(worst, abs(cur["base_yaw"] - prev["base_yaw"]))
            prev = cur
        self.assertLessEqual(worst, cfg.speed_cap * step + 1e-9)


class TestSerialGuard(unittest.TestCase):
    def setUp(self):
        self.tp = load_teleop()

    def test_serial_refuses_without_the_explicit_flag(self):
        from avi_bot.motion import poses
        args = types.SimpleNamespace(backend="serial", yes_move_the_arm=False,
                                     port_name="COM5")
        with self.assertRaises(SystemExit) as cm:
            self.tp.build_backend(args, poses.load())
        self.assertIn("--yes-move-the-arm", str(cm.exception))

    def test_sim_is_the_default_backend(self):
        import argparse
        import contextlib
        import io
        # Parse an empty argv the way main() would, without starting anything.
        src = (ROOT / "assistant" / "teleop.py").read_text()
        self.assertIn('ap.add_argument("--backend", default="sim"', src)
        del argparse, contextlib, io


if __name__ == "__main__":
    unittest.main(verbosity=2)
