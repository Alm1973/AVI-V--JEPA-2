"""Attention: what to look at, and whether following it converges.

BUILD_SPEC 5.3 for the choosing, and a replay of the closed loop for the
following. The convergence test is the one that matters: a sign error in a
feedback loop does not produce a wrong position, it produces a runaway, and the
only way to catch that is to run the loop.
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.motion import engine, poses                 # noqa: E402
from avi_bot.personality import attention as att         # noqa: E402

W, H = 1280, 720


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make(seed=5):
    c = Clock()
    cfg = poses.load()
    a = att.Attention(cfg, clock=c, rng=random.Random(seed))
    return a, c, cfg


class TestChoosing(unittest.TestCase):
    def test_the_priority_order_is_the_spec_s(self):
        self.assertEqual(att.SOURCES,
                         ("speech", "motion", "curiosity", "face", "idle"))

    def test_speech_beats_everything_else(self):
        a, _, _ = make()
        for s in ("face", "motion", "curiosity"):
            a.offer(s, 100, 100, W, H)
        a.offer("speech", 200, 200, W, H)
        self.assertEqual(a.choose().source, "speech")

    def test_a_more_salient_target_wins_outright_not_on_average(self):
        """A gaze that averages two targets looks at neither."""
        a, _, _ = make()
        a.offer("face", 0, H / 2, W, H)          # hard left
        a.offer("speech", W, H / 2, W, H)        # hard right
        t = a.choose()
        self.assertEqual(t.source, "speech")
        self.assertGreater(t.offset[0], 0.4)

    def test_a_stale_target_is_let_go(self):
        a, c, _ = make()
        a.offer("face", 100, 100, W, H)
        self.assertEqual(a.choose().source, "face")
        c.advance(att.STICKY_S["face"] + 0.1)
        self.assertEqual(a.choose().source, "idle")

    def test_one_dropped_detection_does_not_throw_the_gaze_away(self):
        a, c, _ = make()
        a.offer("face", 100, 100, W, H)
        c.advance(att.STICKY_S["face"] * 0.5)
        self.assertEqual(a.choose().source, "face")

    def test_idle_gaze_appears_when_there_is_nothing_to_look_at(self):
        a, _, _ = make()
        t = a.choose(frame=(W, H))
        self.assertEqual(t.source, "idle")
        self.assertLessEqual(abs(t.offset[0]), att.IDLE_YAW_SPREAD + 1e-9)

    def test_idle_gaze_moves_occasionally_not_every_update(self):
        """A new random spot every update reads as a fault, not as looking
        around."""
        a, c, _ = make()
        first = a.choose(frame=(W, H)).offset
        same = [a.choose(frame=(W, H)).offset == first for _ in range(5)]
        self.assertTrue(all(same), "idle gaze jumped on every call")
        c.advance(6.0)
        self.assertNotEqual(a.choose(frame=(W, H)).offset, first)

    def test_an_unknown_source_raises(self):
        a, _, _ = make()
        with self.assertRaises(ValueError):
            a.offer("vibes", 1, 1, W, H)

    def test_dropping_the_current_target_clears_it(self):
        a, _, _ = make()
        a.offer("face", 100, 100, W, H)
        a.choose()
        a.drop("face")
        self.assertIsNone(a.current)


class TestCorrections(unittest.TestCase):
    def test_a_centred_target_commands_nothing(self):
        """Sub-degree jitter the arm cannot act on anyway is worse than
        nothing: it keeps the serial link busy and the joint buzzing."""
        a, _, _ = make()
        a.offer("face", W / 2, H / 2, W, H)
        a.choose()
        self.assertEqual(a.corrections(), (0.0, 0.0))

    def test_a_target_right_of_centre_turns_the_view_right(self):
        a, _, _ = make()
        a.offer("face", W * 0.8, H / 2, W, H)
        a.choose()
        d_yaw, _ = a.corrections()
        # base_yaw's sign in config makes larger degrees look right, confirmed
        # by closing this loop on hardware, so the correction is negative.
        self.assertLess(d_yaw, 0.0)

    def test_a_target_below_centre_tilts_the_view_down(self):
        a, _, _ = make()
        a.offer("face", W / 2, H * 0.85, W, H)
        a.choose()
        _, d_wrist = a.corrections()
        # Higher wrist degrees tilt UP, so looking down is negative.
        self.assertLess(d_wrist, 0.0)

    def test_the_gain_is_below_one(self):
        """The loop runs faster than the arm. Asking for the whole error every
        update commands a move that is still in progress when the next
        correction is computed, which is how a tracker hunts."""
        self.assertLess(att.GAIN, 1.0)

    def test_error_is_reported_in_degrees_for_the_m3_test(self):
        a, _, cfg = make()
        a.offer("face", W, H / 2, W, H)          # half a frame off centre
        a.choose()
        ex, _ = a.error_degrees()
        self.assertAlmostEqual(ex, 0.5 * cfg.camera["yaw_deg_per_frame"],
                               places=3)

    def test_the_gain_comes_from_config_not_a_datasheet(self):
        _, _, cfg = make()
        self.assertAlmostEqual(cfg.camera["yaw_deg_per_frame"], 29.0)


class TestLoopConverges(unittest.TestCase):
    """Replay the loop against a simulated target. A sign error here is a
    runaway, not an offset, so it has to be run rather than reasoned about."""

    def run_loop(self, start_frac, steps=30, gain=att.GAIN, flip=False):
        a, c, cfg = make()
        deg_per_frame = cfg.camera["yaw_deg_per_frame"]
        a.gain = gain
        offset = start_frac
        history = [offset]
        for _ in range(steps):
            a.offer("face", W / 2 + offset * W, H / 2, W, H)
            a.choose()
            d_yaw, _ = a.corrections()
            if flip:
                d_yaw = -d_yaw
            # Moving the view right by d degrees moves the target LEFT in the
            # image by d / deg_per_frame of a frame.
            offset += d_yaw / deg_per_frame
            history.append(offset)
            c.advance(0.2)
        return history

    def test_it_converges_from_either_side(self):
        for start in (0.35, -0.35):
            h = self.run_loop(start)
            self.assertLess(abs(h[-1]), 0.03,
                            f"from {start}: ended at {h[-1]:+.3f}")

    def test_it_does_not_overshoot_past_the_other_side(self):
        h = self.run_loop(0.35)
        self.assertTrue(all(x > -0.05 for x in h),
                        f"overshot: {[round(x, 3) for x in h[:6]]}")

    def test_a_flipped_sign_runs_away(self):
        """The failure this whole exercise exists to catch."""
        h = self.run_loop(0.20, flip=True)
        self.assertGreater(abs(h[-1]), abs(h[0]) * 2,
                           "a flipped sign should diverge, not settle")

    def test_it_holds_within_the_m3_tolerance(self):
        # BUILD_SPEC 15: look-at keeps a person box within +-5 deg of centre.
        _, _, cfg = make()
        h = self.run_loop(0.35, steps=40)
        tail = h[20:]
        worst = max(abs(x) * cfg.camera["yaw_deg_per_frame"] for x in tail)
        self.assertLess(worst, 5.0, f"settles {worst:.1f} deg off centre")

    def test_a_gain_of_one_still_settles(self):
        """It should be stable even if someone turns the gain all the way up,
        because the alternative is a tracker that hunts forever."""
        h = self.run_loop(0.30, gain=1.0, steps=30)
        self.assertLess(abs(h[-1]), 0.05)


class TestSteering(unittest.TestCase):
    def test_steering_moves_the_spring_not_the_joint(self):
        """A jittery detection must not become a jittery arm."""
        a, _, cfg = make()
        layer = engine.LookAt()
        a.offer("face", W * 0.8, H / 2, W, H)
        a.choose()
        before = dict(layer.value)
        a.steer(layer)
        self.assertNotEqual(layer.target["base_yaw"], 0.0)
        self.assertEqual(layer.value, before, "steer moved the joint directly")

    def test_steering_twice_on_one_detection_corrects_once(self):
        """The bug that only showed on hardware. steer() is called from a
        50 Hz motion loop while detections arrive at about 2.5 Hz, so without
        this it integrates the same error twenty times and winds the target to
        its bound in a fraction of a second -- which looks exactly like a gain
        problem and is not one."""
        a, _, _ = make()
        layer = engine.LookAt()
        a.offer("face", W * 0.85, H / 2, W, H)
        a.choose()
        first, _ = a.steer(layer)
        self.assertNotEqual(first, 0.0)
        after_one = layer.target["base_yaw"]
        for _ in range(20):
            self.assertEqual(a.steer(layer), (0.0, 0.0))
        self.assertEqual(layer.target["base_yaw"], after_one,
                         "integrated a stale detection")

    def test_a_new_detection_steers_again(self):
        a, c, _ = make()
        layer = engine.LookAt()
        a.offer("face", W * 0.85, H / 2, W, H)
        a.choose()
        a.steer(layer)
        one = layer.target["base_yaw"]
        c.advance(0.4)
        a.offer("face", W * 0.85, H / 2, W, H)     # fresh measurement
        a.choose()
        a.steer(layer)
        self.assertLess(layer.target["base_yaw"], one)

    def test_the_target_cannot_wind_up_without_bound(self):
        a, c, _ = make()
        layer = engine.LookAt()
        for _ in range(200):
            c.advance(0.4)
            a.offer("face", W, H / 2, W, H)        # permanently off-centre
            a.choose()
            a.steer(layer)
        self.assertLessEqual(abs(layer.target["base_yaw"]),
                             att.MAX_TARGET_DEG + 1e-9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
