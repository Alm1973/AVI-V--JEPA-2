"""M1 motion foundation tests. No hardware, no GPU, no serial port.

These run on the Mac. The ones that matter most are:
  - no jump on startup (the thing that would scare someone at the desk)
  - every gesture stays inside the soft limits
  - every gesture's own curve fits the firmware speed cap, so the limiter is
    never silently smearing a gesture into something that reads differently
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.motion import backends, easing, engine, gestures, poses, safety


def peak_rate(move, start="HOME", hz=400):
    """Highest per-joint angular rate the move's own curve asks for, deg/s.

    Sampled at 400 Hz, well above the 50 Hz the engine runs at, so a short
    fast segment cannot hide between two samples.
    """
    move.start(poses.get_pose(start))
    dur = move.duration if move.duration > 0 else 1.0
    step = 1.0 / hz
    prev = None
    worst = 0.0
    t = 0.0
    while t <= dur + step:
        base, delta = move.sample(t)
        cur = {j: (base or poses.get_pose(start))[j] + delta[j]
               for j in poses.JOINTS}
        if prev is not None:
            for j in poses.JOINTS:
                worst = max(worst, abs(cur[j] - prev[j]) / step)
        prev = cur
        t += step
    return worst


def all_gestures():
    for name in sorted(gestures.GESTURES):
        yield name, gestures.make(name)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.c = poses.load()

    def test_channel_order_matches_wire_protocol(self):
        self.assertEqual([self.c.joints[j]["channel"] for j in poses.JOINTS],
                         [0, 1, 2, 3])

    def test_piecewise_map_hits_the_calibration_points(self):
        """0 and 180 degrees hit the calibrated ends, and center always hits
        center. A reversed joint swaps which end is which -- that is the whole
        meaning of its sign."""
        for j in poses.JOINTS:
            cfg = self.c.joints[j]
            ends = (cfg["min_count"], cfg["max_count"])
            if self.c.sign(j) < 0:
                ends = tuple(reversed(ends))
            self.assertAlmostEqual(self.c.deg_to_count(j, 0), ends[0], msg=j)
            self.assertAlmostEqual(self.c.deg_to_count(j, 90),
                                   cfg["center_count"], msg=j)
            self.assertAlmostEqual(self.c.deg_to_count(j, 180), ends[1], msg=j)

    def test_map_is_monotonic(self):
        """Monotonic in one direction or the other, never both. A non-monotonic
        map would mean two different angles drive the servo to the same place."""
        for j in poses.JOINTS:
            vals = [self.c.deg_to_count(j, d) for d in range(0, 181)]
            want = sorted(vals, reverse=self.c.sign(j) < 0)
            self.assertEqual(vals, want, f"{j} map is not monotonic")

    def test_soft_limits_sit_inside_the_calibrated_range(self):
        for j in poses.JOINTS:
            lo, hi = self.c.soft_limits(j)
            self.assertGreater(lo, 0.0, j)
            self.assertLess(hi, 180.0, j)
            self.assertLess(lo, hi, j)

    def test_every_named_pose_is_inside_the_soft_limits(self):
        for name in poses.POSES:
            p = poses.get_pose(name)
            for j in poses.JOINTS:
                lo, hi = self.c.soft_limits(j)
                self.assertTrue(lo <= p[j] <= hi,
                                f"pose {name} joint {j} = {p[j]} outside {lo}..{hi}")

    def test_observe_is_the_calibrated_center(self):
        # The observer only logs object positions from OBSERVE, so if this
        # drifts, every "left side of the desk" in the database is wrong.
        self.assertEqual(poses.get_pose("OBSERVE"), poses.pose())


class TestEasing(unittest.TestCase):
    def test_endpoints(self):
        for name, f in easing.BY_NAME.items():
            self.assertAlmostEqual(f(0.0), 0.0, places=9, msg=name)
            self.assertAlmostEqual(f(1.0), 1.0, places=9, msg=name)

    def test_back_curves_leave_the_unit_range(self):
        # This is what makes anticipation and overshoot work. If a refactor
        # clamps these, perk_up stops reading as eager.
        self.assertLess(min(easing.ease_in_back(t / 100) for t in range(101)), 0.0)
        self.assertGreater(max(easing.ease_out_back(t / 100) for t in range(101)), 1.0)

    def test_calm_curves_stay_in_range(self):
        for name in ("linear", "ease_in", "ease_out", "ease_in_out"):
            f = easing.BY_NAME[name]
            for i in range(101):
                self.assertTrue(-1e-9 <= f(i / 100) <= 1 + 1e-9, name)


class TestLimiter(unittest.TestCase):
    def setUp(self):
        self.c = poses.load()

    def test_clamps_to_soft_limits(self):
        lim = safety.Limiter(self.c, drive_undriven=True)
        out = lim.apply(poses.pose(base_yaw=999, shoulder=-999,
                                   elbow=999, wrist=-999), dt=10.0)
        for j in poses.JOINTS:
            lo, hi = self.c.soft_limits(j)
            self.assertTrue(lo <= out[j] <= hi, f"{j}={out[j]}")

    def test_speed_cap(self):
        lim = safety.Limiter(self.c, drive_undriven=True)
        dt = 1.0 / self.c.tick_hz
        out = lim.apply(poses.pose(base_yaw=160.0), dt=dt)
        self.assertAlmostEqual(out["base_yaw"], 90.0 + self.c.speed_cap * dt,
                               places=6)

    def test_slew_is_measured_against_what_was_emitted(self):
        """A long run of clamped requests must not store up into a jump.

        If the limiter measured against the request instead, releasing the
        clamp would let the arm leap the whole accumulated distance at once.
        """
        lim = safety.Limiter(self.c, drive_undriven=True)
        dt = 1.0 / self.c.tick_hz
        for _ in range(100):
            lim.apply(poses.pose(base_yaw=999.0), dt=dt)
        before = lim.last["base_yaw"]
        out = lim.apply(poses.pose(base_yaw=90.0), dt=dt)
        self.assertLessEqual(abs(out["base_yaw"] - before),
                             self.c.speed_cap * dt + 1e-9)

    def test_undriven_joints_are_pinned_by_default(self):
        lim = safety.Limiter(self.c)
        out = lim.apply(poses.pose(elbow=140.0, wrist=40.0), dt=1.0)
        self.assertEqual(out["elbow"], 90.0)
        self.assertEqual(out["wrist"], 90.0)
        self.assertIn("elbow", lim.clamped_joints)


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.c = poses.load()
        self.sim = backends.SimBackend(self.c)

    def test_no_jump_on_startup(self):
        for start in ("HOME", "REST", "LOW"):
            sim = backends.SimBackend(self.c)
            e = engine.MotionEngine(self.c, sim, start_pose=start)
            self.assertEqual(e.tick(0.0), poses.get_pose(start),
                             f"first command from {start} was not {start}")

    def test_first_command_never_moves_further_than_one_tick(self):
        e = engine.MotionEngine(self.c, self.sim, start_pose="REST")
        e.push(gestures.yawn_wake())
        first = e.tick(0.0)
        self.assertLessEqual(
            max(abs(first[j] - poses.get_pose("REST")[j]) for j in poses.JOINTS),
            self.c.speed_cap / self.c.tick_hz + 1e-9)

    def test_crossfade_has_no_discontinuity(self):
        """Interrupt a gesture mid-flight and the output must stay smooth."""
        e = engine.MotionEngine(self.c, self.sim, drive_undriven=True)
        step = 1.0 / self.c.tick_hz
        t = 0.0
        e.push(gestures.shake())
        for _ in range(20):
            e.tick(t); t += step
        e.push(gestures.perk_up())          # interrupt halfway through
        prev = dict(e.last_sent)
        worst = 0.0
        for _ in range(60):
            cur = e.tick(t); t += step
            worst = max(worst, max(abs(cur[j] - prev[j]) for j in poses.JOINTS))
            prev = cur
        self.assertLessEqual(worst, self.c.speed_cap * step + 1e-9,
                             f"cross-fade produced a {worst:.2f} deg jump")

    def test_pose_move_lands_on_its_target(self):
        e = engine.MotionEngine(self.c, self.sim)
        e.push(gestures.perk_up())
        e.run(2.0)
        self.assertAlmostEqual(e.base["shoulder"],
                               poses.get_pose("TALL")["shoulder"], places=3)

    def test_look_at_converges_without_running_away(self):
        layer = engine.LookAt()
        layer.weight = 1.0
        layer.set_target(yaw=10.0)
        style = engine.Style()
        peak = 0.0
        t = 0.0
        for _ in range(500):
            peak = max(peak, layer.sample(t, style)["base_yaw"])
            t += 0.02
        self.assertAlmostEqual(layer.value["base_yaw"], 10.0, delta=0.2)
        self.assertLess(peak, 14.0, "look-at overshoot is too springy")

    def test_suppress_is_called_so_self_motion_is_not_a_scene_change(self):
        seen = []
        e = engine.MotionEngine(self.c, self.sim, suppress_cb=seen.append)
        move = gestures.shake()
        e.push(move)
        e.tick(5.0)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], 5.0 + move.duration + 0.5, places=6)

    def test_thirty_minutes_unattended_stays_legal(self):
        e = engine.MotionEngine(self.c, self.sim)
        for layer in e.layers:
            layer.weight = 1.0
        e.run(1800.0)
        self.assertGreater(e.ticks, 89000)
        self.assertLessEqual(len(self.sim.history), 4096, "history is unbounded")
        for _, p in self.sim.history:
            for j in poses.JOINTS:
                lo, hi = self.c.soft_limits(j)
                self.assertTrue(lo <= p[j] <= hi, f"{j}={p[j]}")


class TestGestures(unittest.TestCase):
    def setUp(self):
        self.c = poses.load()

    def test_every_gesture_runs_in_sim_within_limits(self):
        for name, move in all_gestures():
            with self.subTest(gesture=name):
                sim = backends.SimBackend(self.c)
                e = engine.MotionEngine(self.c, sim, drive_undriven=True)
                e.push(move)
                e.run(max(3.0, move.duration + 1.0))
                for _, p in sim.history:
                    for j in poses.JOINTS:
                        lo, hi = self.c.soft_limits(j)
                        self.assertTrue(lo <= p[j] <= hi, f"{name}: {j}={p[j]}")

    def test_every_gesture_fits_the_firmware_speed_cap(self):
        """A gesture that asks for more than the cap gets silently smeared.

        It would still be safe, but it would not look like the gesture any
        more: a shake that cannot reach its reversal in time reads as a sway.
        """
        over = {}
        for name, move in all_gestures():
            rate = peak_rate(move, gestures.START_POSE.get(name, "HOME"))
            if rate > self.c.speed_cap + 1e-6:
                over[name] = round(rate, 1)
        self.assertEqual(over, {}, f"gestures exceeding "
                                   f"{self.c.speed_cap:.0f} deg/s: {over}")

    def test_ahem_scales_its_turn_with_the_angle(self):
        for yaw in (0.0, 10.0, 25.0, 40.0, -40.0):
            rate = peak_rate(gestures.ahem(yaw=yaw))
            self.assertLessEqual(rate, self.c.speed_cap + 1e-6,
                                 f"ahem(yaw={yaw}) peaks at {rate:.1f} deg/s")

    def test_each_gesture_actually_moves_something(self):
        """Guards against a gesture whose keyframes cancel out to nothing."""
        for name, move in all_gestures():
            with self.subTest(gesture=name):
                sim = backends.SimBackend(self.c)
                start = gestures.START_POSE.get(name, "HOME")
                e = engine.MotionEngine(self.c, sim, start_pose=start,
                                        drive_undriven=True)
                for layer in e.layers:
                    layer.weight = 0.0      # isolate the gesture from breathing
                e.push(move)
                e.run(max(3.0, move.duration + 1.0))
                origin = poses.get_pose(start)
                spread = max(
                    max(abs(p[j] - origin[j]) for j in poses.JOINTS)
                    for _, p in sim.history)
                self.assertGreater(spread, 1.0,
                                   f"{name} produced no motion at all")

    def test_wrist_only_gestures_are_declared_as_no_ops(self):
        """Keep the docstring's claim honest as the config changes."""
        for name in gestures.NO_OP_UNTIL_M1:
            sim = backends.SimBackend(self.c)
            e = engine.MotionEngine(self.c, sim)      # hardware-faithful pinning
            for layer in e.layers:
                layer.weight = 0.0
            e.push(gestures.make(name))
            e.run(3.0)
            spread = max(max(abs(p[j] - 90.0) for j in poses.JOINTS)
                         for _, p in sim.history)
            self.assertLess(spread, 1.0,
                            f"{name} is listed in NO_OP_UNTIL_M1 but it moves")

    def test_undriven_joints_never_move_on_the_hardware_path(self):
        for name, move in all_gestures():
            with self.subTest(gesture=name):
                sim = backends.SimBackend(self.c)
                e = engine.MotionEngine(self.c, sim)   # drive_undriven=False
                e.push(move)
                e.run(max(3.0, move.duration + 1.0))
                for _, p in sim.history:
                    self.assertEqual(p["elbow"], 90.0, name)
                    self.assertEqual(p["wrist"], 90.0, name)


class TestBackends(unittest.TestCase):
    def test_dry_run_records_commands(self):
        c = poses.load()
        b = backends.DryRunBackend()
        engine.MotionEngine(c, b).run(0.1)
        self.assertEqual(len(b.commands), 5)

    def test_serial_backend_does_not_open_the_port_on_construction(self):
        # Opening COM5 resets the Uno and drives the servos. Constructing the
        # object must never do that by accident.
        c = poses.load()
        b = backends.SerialBackend("COM5", c)
        self.assertIsNone(b.ser)
        with self.assertRaises(RuntimeError):
            b.send(poses.get_pose("HOME"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
