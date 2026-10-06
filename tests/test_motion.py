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


def config_with_pinned(joint="wrist"):
    """The real config, with one joint forced undriven.

    The pinning mechanism has to keep working even when every joint in
    config/joints.toml happens to be driven -- which is now the case. Testing
    it against the live config would simply stop testing anything, so the
    fixture supplies the condition instead.
    """
    import copy
    import tomllib
    with open(poses.CONFIG_PATH, "rb") as f:
        data = copy.deepcopy(tomllib.load(f))
    data["joints"][joint]["driven"] = False
    data["joints"][joint]["sign"] = 0
    return poses.JointConfig(data), joint


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

    def test_observe_is_fully_specified(self):
        """The observer logs object positions ONLY from OBSERVE, so if it is
        ever partly specified the arm ends up somewhere nobody chose and every
        "left side of the desk" in the database means something different.

        It is deliberately NOT the mechanical centre: measured 2026-10-05, the
        camera at wrist centre points at the carpet, so a centred OBSERVE would
        have the logger faithfully recording the floor.
        """
        p = poses.get_pose("OBSERVE")
        self.assertEqual(set(p), set(poses.JOINTS))
        for j in poses.JOINTS:
            self.assertIsInstance(p[j], float)
        if poses.POSES_ARE_TAUGHT:
            self.assertNotEqual(p, poses.pose(),
                                "OBSERVE is at mechanical centre, which aims "
                                "at the carpet; re-teach it")

    def test_the_poses_in_use_were_taught_on_the_arm(self):
        """Not a rule, a record. If config/poses.toml goes missing the fallback
        table takes over, and those values are guesses."""
        self.assertTrue(poses.POSES_ARE_TAUGHT,
                        "config/poses.toml is missing, so the invented "
                        "fallback poses are in use")

    def test_distinct_pose_names_are_actually_distinct(self):
        """Two names for one position is almost always an unfinished teach --
        and a gesture that moves to it becomes a silent no-op."""
        same = []
        names = sorted(poses.POSES)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if poses.get_pose(a) == poses.get_pose(b):
                    same.append(f"{a}=={b}")
        unexpected = [s for s in same
                      if s not in poses.KNOWN_DUPLICATE_POSES]
        self.assertEqual(unexpected, [],
                         "identical poses that are not listed as known in "
                         "config/poses.toml: " + ", ".join(unexpected))

    def test_known_duplicate_list_does_not_go_stale(self):
        """Once a pose is re-taught, its entry here should go, or the list
        quietly licenses a future mistake."""
        for entry in poses.KNOWN_DUPLICATE_POSES:
            a, _, b = entry.partition("==")
            self.assertEqual(
                poses.get_pose(a), poses.get_pose(b),
                f"{entry} is listed as a known duplicate but the two poses "
                f"now differ; remove it from config/poses.toml")


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

    def test_an_undriven_joint_is_pinned_to_centre(self):
        """A joint with an unknown direction sign must not move at all: a wrong
        sign could drive it into its own end stop."""
        for joint in poses.JOINTS:
            cfg, pinned = config_with_pinned(joint)
            lim = safety.Limiter(cfg)
            out = lim.apply(poses.pose(**{pinned: 140.0}), dt=1.0)
            self.assertEqual(out[pinned], 90.0, pinned)
            self.assertIn(pinned, lim.clamped_joints)

    def test_every_joint_is_driven_right_now(self):
        """Not a rule, a record. If a joint is ever pinned again this fails and
        whoever did it has to say why here."""
        undriven = [j for j in poses.JOINTS if not self.c.is_driven(j)]
        self.assertEqual(undriven, [],
                         "a joint was pinned again; say why, and note that "
                         "poses taught on the real arm may include it")


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
        """Run until the move reports it is finished, not for a fixed time.

        A pose move stretches its own duration to fit what the arm can do, so
        any hardcoded wait here is a guess that breaks the moment the measured
        speed changes.
        """
        e = engine.MotionEngine(self.c, self.sim)
        e.push(gestures.perk_up())
        step = 1.0 / self.c.tick_hz
        now = 0.0
        while e.busy() and now < 30.0:
            e.tick(now)
            now += step
        self.assertFalse(e.busy(), "perk_up never finished")
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
                e.tick(0.0)                      # start it, so it sizes itself
                e.run(max(4.0, move.duration + 1.0), now=1.0 / self.c.tick_hz)
                for _, p in sim.history:
                    for j in poses.JOINTS:
                        lo, hi = self.c.soft_limits(j)
                        self.assertTrue(lo <= p[j] <= hi, f"{name}: {j}={p[j]}")

    def test_every_gesture_fits_what_the_arm_can_actually_do(self):
        """Not the safety cap -- the MEASURED ceiling.

        A gesture that asks for more is not refused, it is silently smeared
        into a different shape: a shake that cannot reach its reversal in time
        reads as a sway, and nothing in the logs says so.
        """
        over = {}
        for name, move in all_gestures():
            rate = peak_rate(move, gestures.START_POSE.get(name, "HOME"))
            if rate > self.c.achievable + 0.5:
                over[name] = round(rate, 1)
        self.assertEqual(over, {}, f"gestures exceeding the measured "
                                   f"{self.c.achievable:.0f} deg/s: {over}")

    def test_the_safety_cap_is_above_what_the_arm_can_reach(self):
        """If the clamp were below the arm's ability it would be the thing
        limiting motion, and the measured ceiling would be meaningless."""
        self.assertGreater(self.c.speed_cap, self.c.achievable)

    def test_raising_the_achievable_rate_restores_amplitude(self):
        """The whole point of deriving from the measurement: when the servo
        supply is fixed and the number goes up, the gestures get their size
        back without anyone editing a gesture."""
        slow = engine.KeyframeMove("t", [(0.0, {"base_yaw": 0.0}),
                                         (0.5, {"base_yaw": 10.0}),
                                         (1.0, {"base_yaw": 0.0})],
                                   0.4, "linear", max_rate=28.0)
        fast = engine.KeyframeMove("t", [(0.0, {"base_yaw": 0.0}),
                                         (0.5, {"base_yaw": 10.0}),
                                         (1.0, {"base_yaw": 0.0})],
                                   0.4, "linear", max_rate=120.0)
        self.assertLess(slow.scale, fast.scale)
        self.assertAlmostEqual(fast.scale, 1.0)
        self.assertAlmostEqual(slow.peak_rate(), 28.0, delta=0.5)

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
                e.tick(0.0)
                e.run(max(4.0, move.duration + 1.0), now=1.0 / self.c.tick_hz)
                origin = poses.get_pose(start)
                spread = max(
                    max(abs(p[j] - origin[j]) for j in poses.JOINTS)
                    for _, p in sim.history)
                self.assertGreater(spread, 1.0,
                                   f"{name} produced no motion at all")

    def test_declared_no_ops_really_are_no_ops(self):
        """Keep the declaration honest as the config changes. Empty is fine --
        it means every gesture now moves something on hardware."""
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

    def test_a_pinned_joint_never_moves_however_a_gesture_asks(self):
        """Gestures do not get to override the pin, whatever their keyframes
        say. Checked against a synthetic pinned joint so it keeps testing the
        mechanism once every real joint is driven."""
        cfg, pinned = config_with_pinned("wrist")
        for name, move in all_gestures():
            with self.subTest(gesture=name):
                sim = backends.SimBackend(cfg)
                e = engine.MotionEngine(cfg, sim)      # drive_undriven=False
                e.push(move)
                e.tick(0.0)
                e.run(max(4.0, move.duration + 1.0), now=1.0 / cfg.tick_hz)
                for _, p in sim.history:
                    self.assertEqual(p[pinned], 90.0, f"{name}: {pinned} moved")


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
        self.assertFalse(b.healthy)

    def test_sending_never_opens_a_port_that_was_never_opened(self):
        """send() reconnects a port that has dropped, which must not become a
        way of opening one for the first time: that resets the Uno and moves
        the servos with nobody expecting it."""
        c = poses.load()
        b = backends.SerialBackend("COM5", c)
        opened = []
        b.open = lambda: opened.append(True)
        self.assertFalse(b.send(poses.get_pose("HOME")))
        self.assertEqual(opened, [], "send() tried to open the port itself")
        self.assertIsNone(b.ser)

    def test_a_dropped_write_is_reported_not_raised(self):
        """The caller's loop must not die on one bad frame. A dropped frame is
        harmless: the target is repeated continuously and the firmware holds
        position if it hears nothing."""
        c = poses.load()
        b = backends.SerialBackend("COM5", c)
        b._was_opened = True
        b.healthy = True

        class Boom:
            def write(self, _):
                raise OSError("WriteFile failed")

            def close(self):
                pass

        b.ser = Boom()
        self.assertFalse(b.send(poses.get_pose("HOME")))
        self.assertFalse(b.healthy)
        self.assertIn("WriteFile failed", b.last_error)
        self.assertIsNone(b.ser, "a failed port should be dropped, not kept")


if __name__ == "__main__":
    unittest.main(verbosity=2)
