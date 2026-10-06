"""The dome: look directions with TALL at the top.

The frame is anchored to poses Shaurya taught by hand, so these check two
different things: that the geometry is self-consistent (a direction converts
to joints and back), and that the frame agrees with what he SAID each pose was
for. The second is the real test -- the poses were taught on different days
with no model in mind, so their agreeing is evidence rather than arithmetic.
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import dome as dome_mod                # noqa: E402
from avi_bot.motion import poses as poses_mod                  # noqa: E402

POSES = {
    "HOME": {"base_yaw": 90.0, "shoulder": 72.5, "elbow": 48.5, "wrist": 87.5},
    "OBSERVE": {"base_yaw": 60.0, "shoulder": 84.0, "elbow": 95.0,
                "wrist": 147.9},
    "ATTENTIVE": {"base_yaw": 90.0, "shoulder": 29.5, "elbow": 131.0,
                  "wrist": 131.0},
    "TALL": {"base_yaw": 90.0, "shoulder": 78.4, "elbow": 88.0,
             "wrist": 176.0},
    "LOW": {"base_yaw": 90.0, "shoulder": 154.0, "elbow": 30.0,
            "wrist": 129.0},
    "REST": {"base_yaw": 90.0, "shoulder": 150.0, "elbow": 140.5,
             "wrist": 30.5},
}

LIMITS = {"base_yaw": (20.0, 160.0), "shoulder": (25.0, 155.0),
          "elbow": (20.0, 160.0), "wrist": (20.0, 176.0)}


def a_dome(**kw):
    kw.setdefault("limits", LIMITS)
    return dome_mod.Dome(POSES, **kw)


class TallIsTheTop(unittest.TestCase):
    def test_the_pole_is_at_zero_elevation(self):
        self.assertAlmostEqual(a_dome().elevation_of(POSES["TALL"]), 0.0,
                               places=9)

    def test_every_other_taught_pose_is_below_it(self):
        d = a_dome()
        for name, pose in POSES.items():
            if name == "TALL":
                continue
            with self.subTest(name):
                self.assertGreater(d.elevation_of(pose), 0.0,
                                   f"{name} came out above TALL")

    def test_the_pole_sits_on_the_wrists_limit(self):
        """TALL is the top because the arm cannot look higher, not because it
        was labelled that way. Its wrist IS the wrist's soft max."""
        self.assertEqual(POSES["TALL"]["wrist"], LIMITS["wrist"][1])

    def test_nothing_above_the_pole_is_describable(self):
        above = dict(POSES["TALL"], wrist=180.0)
        with self.assertRaises(dome_mod.OffDome) as cm:
            a_dome().direction_of(above)
        self.assertIn("above the pole", str(cm.exception))


class ItAgreesWithWhatHeCalledThem(unittest.TestCase):
    """The poses were taught by hand on different days with no model in mind.
    That the model orders them the way he described them is the evidence the
    inferred signs are right."""

    def test_the_order_matches_the_descriptions(self):
        d = a_dome()
        order = sorted((n for n in POSES),
                       key=lambda n: d.elevation_of(POSES[n]))
        self.assertEqual(order[0], "TALL", "alert and upright is the top")
        self.assertEqual(order[-1], "REST", "asleep, camera down, is the floor")
        self.assertLess(order.index("ATTENTIVE"), order.index("LOW"))

    def test_the_three_desk_poses_point_within_five_degrees(self):
        """ATTENTIVE, OBSERVE and HOME are all aimed at the desk, and the
        model says so without being told: -39.1, -40.7, -43.1."""
        d = a_dome()
        els = [d.elevation_of(POSES[n])
               for n in ("ATTENTIVE", "OBSERVE", "HOME")]
        self.assertLess(max(els) - min(els), 5.0, els)

    def test_rest_is_outside_the_model_and_says_so(self):
        """Folded back on itself, a linear sum of orientations stops meaning a
        direction. -269 degrees is arithmetic, not somewhere to look."""
        with self.assertRaises(dome_mod.OffDome) as cm:
            a_dome().direction_of(POSES["REST"])
        self.assertIn("folded", str(cm.exception))

    def test_only_one_sign_combination_does_this(self):
        """If another combination also worked, the agreement above would be
        luck rather than evidence."""
        works = []
        for sh in (-1.0, 1.0):
            for el in (-1.0, 1.0):
                d = a_dome(signs={"shoulder": sh, "elbow": el, "wrist": 1.0})
                pitch = {n: d._pitch(p) for n, p in POSES.items()}
                order = sorted(pitch, key=lambda n: -pitch[n])
                if order[0] == "TALL" and order[-1] == "REST":
                    works.append((sh, el))
        self.assertEqual(works, [(-1.0, -1.0)])


class TheFrameIsConsistent(unittest.TestCase):
    def test_a_direction_converts_to_joints_and_back(self):
        d = a_dome()
        for az, el in ((0.0, 0.0), (10.0, 20.0), (-25.0, 45.0), (0.0, 60.0)):
            with self.subTest(az=az, el=el):
                j = d.joints_for(az, el)
                got_az, got_el = d.direction_of(j)
                self.assertAlmostEqual(got_el, el, places=3)
                self.assertAlmostEqual(got_az, az, places=3)

    def test_azimuth_is_zero_at_the_poles_base_yaw(self):
        self.assertAlmostEqual(a_dome().azimuth_of(POSES["TALL"]), 0.0,
                               places=9)

    def test_angles_use_spherical_geometry_not_subtraction(self):
        """Two directions 60 degrees below the pole and 30 apart in azimuth
        are much closer than 30 degrees. Subtracting coordinates would miss
        that and a 'nearest place' lookup would pick the wrong one."""
        d = a_dome()
        a = d.joints_for(0.0, 60.0)
        b = d.joints_for(30.0, 60.0)
        self.assertLess(d.angle_between(a, b), 30.0)
        self.assertGreater(d.angle_between(a, b), 20.0)

    def test_near_the_pole_azimuth_barely_matters(self):
        """At the top of a dome every azimuth is the same place, so panning
        does almost nothing when the arm is near TALL. Uses a REACHABLE
        azimuth: the dome is only about 87 degrees wide."""
        d = a_dome()
        lo, hi = d.azimuth_limits()
        near = d.joints_for(0.0, 1.0)
        far = d.joints_for(hi - 1.0, 1.0)
        self.assertLess(d.angle_between(near, far), 3.0)

    def test_the_reachable_azimuth_is_a_patch_not_a_hemisphere(self):
        """base_yaw spans 140 degrees of command but the view turns by the
        gain times that. Worth pinning because every sweep plan depends on it
        and 360 degrees is the natural wrong assumption."""
        lo, hi = a_dome().azimuth_limits()
        self.assertAlmostEqual(hi - lo, 140.0 * 0.62, places=3)
        self.assertLess(hi - lo, 90.0)


class RedundancyIsResolvedTowardTaughtShapes(unittest.TestCase):
    """Three pitch joints serve one degree of elevation, so a direction has
    infinitely many solutions. Choosing the one nearest a taught pose keeps
    the arm in shapes he picked by hand."""

    def test_it_stays_near_the_preferred_pose(self):
        d = a_dome()
        el = d.elevation_of(POSES["OBSERVE"])
        j = d.joints_for(0.0, el, prefer="OBSERVE")
        for joint in ("shoulder", "elbow", "wrist"):
            with self.subTest(joint):
                self.assertLess(abs(j[joint] - POSES["OBSERVE"][joint]), 15.0)

    def test_a_different_preference_gives_a_different_shape(self):
        d = a_dome()
        el = 45.0
        a = d.joints_for(0.0, el, prefer="OBSERVE")
        b = d.joints_for(0.0, el, prefer="ATTENTIVE")
        self.assertGreater(abs(a["shoulder"] - b["shoulder"]), 5.0)
        # Same direction all the same, which is the point of redundancy.
        self.assertAlmostEqual(d.elevation_of(a), d.elevation_of(b), places=3)

    def test_every_solution_respects_the_soft_limits(self):
        d = a_dome()
        for el in (0.0, 15.0, 30.0, 50.0, 70.0, 90.0):
            for prefer in ("TALL", "OBSERVE", "ATTENTIVE"):
                try:
                    j = d.joints_for(0.0, el, prefer=prefer)
                except dome_mod.OffDome:
                    continue
                for name, (lo, hi) in LIMITS.items():
                    with self.subTest(el=el, prefer=prefer, joint=name):
                        self.assertGreaterEqual(j[name], lo - 1e-6)
                        self.assertLessEqual(j[name], hi + 1e-6)

    def test_an_unreachable_elevation_raises_rather_than_clamping(self):
        """A clamped answer makes the robot stare confidently at the wrong
        thing, which is the same failure roommap.look_at refuses."""
        d = a_dome()
        with self.assertRaises(dome_mod.OffDome):
            d.joints_for(0.0, 500.0)


class Centring(unittest.TestCase):
    def test_something_off_centre_comes_back_to_the_middle(self):
        d = a_dome()
        seen = POSES["OBSERVE"]
        want = d.look_at(seen, cx=1000, cy=500)
        # The new pose must look further round and further down than before.
        az0, el0 = d.direction_of(seen)
        az1, el1 = d.direction_of(want)
        self.assertGreater(az1, az0)
        self.assertGreater(el1, el0)

    def test_a_centred_object_needs_no_move(self):
        d = a_dome()
        seen = POSES["OBSERVE"]
        want = d.look_at(seen, cx=640, cy=360)
        for j in ("base_yaw", "shoulder", "elbow", "wrist"):
            with self.subTest(j):
                self.assertAlmostEqual(want[j], seen[j], places=3)


class LearningFromItsOwnMotion(unittest.TestCase):
    """Moving a joint by a known amount and watching how far a fixed landmark
    slid is a direct measurement of that joint's effect on the look
    direction. This is the wrist measurement generalised to every joint."""

    def samples(self, gains, signs=None, frame=(1280, 720), fov=(52.0, 31.0)):
        signs = signs or dome_mod.PITCH_SIGNS
        w, h = frame
        out = []
        # Move each joint ALONE, so the three pitch gains are separable.
        for joint in ("base_yaw", "shoulder", "elbow", "wrist"):
            for step in (-8.0, -4.0, 4.0, 8.0):
                before = dict(POSES["OBSERVE"])
                after = dict(before, **{joint: before[joint] + step})
                if joint == "base_yaw":
                    dx = -(gains["base_yaw"] * step / fov[0]) * w
                    dy = 0.0
                else:
                    dx = 0.0
                    dy = (signs[joint] * gains[joint] * step / fov[1]) * h
                out.append((before, after, dx, dy))
        return out

    def test_it_recovers_gains_it_was_never_told(self):
        truth = {"base_yaw": 0.74, "shoulder": 0.91, "elbow": 1.08,
                 "wrist": 0.96}
        d = a_dome(gains={"base_yaw": 0.1, "shoulder": 0.1, "elbow": 0.1,
                          "wrist": 0.1})
        got = d.learn(self.samples(truth))
        for j, want in truth.items():
            with self.subTest(j):
                self.assertAlmostEqual(got[j], want, places=3)

    def test_learning_marks_them_as_no_longer_assumed(self):
        d = a_dome()
        self.assertIn("shoulder", d.unknown())
        d.learn(self.samples(dome_mod.DEFAULT_GAINS))
        self.assertNotIn("shoulder", d.unknown())

    def test_joints_moved_together_are_not_separable_and_it_refuses(self):
        """If every sample moved shoulder and elbow by the same amount, their
        individual gains are not identifiable and a fit would return an
        arbitrary split of one real number."""
        d = a_dome()
        both = []
        for step in (-8.0, -4.0, 4.0, 8.0):
            before = dict(POSES["OBSERVE"])
            after = dict(before, shoulder=before["shoulder"] + step,
                         elbow=before["elbow"] + step)
            both.append((before, after, 0.0, 10.0 * step))
        got = d.learn(both)
        for j in ("shoulder", "elbow", "wrist"):
            self.assertNotIn(j, got)

    def test_too_few_samples_fits_nothing(self):
        d = a_dome()
        self.assertEqual(d.learn(self.samples(dome_mod.DEFAULT_GAINS)[:1]), {})

    def test_the_unmeasured_gains_are_named_as_such(self):
        """shoulder and elbow have never been measured on the hardware. A
        guess that behaves like a measurement is how 29.0 deg-per-frame
        survived for weeks."""
        unknown = a_dome().unknown()
        self.assertIn("never measured", unknown["shoulder"])
        self.assertIn("never measured", unknown["elbow"])


class Rings(unittest.TestCase):
    def test_a_ring_holds_its_elevation(self):
        d = a_dome()
        ring = d.survey_ring(40.0, 8, prefer="OBSERVE")
        self.assertGreater(len(ring), 3)
        for az, j in ring:
            with self.subTest(az=az):
                self.assertAlmostEqual(d.elevation_of(j), 40.0, places=3)

    def test_a_ring_stays_inside_the_reachable_azimuth(self):
        d = a_dome()
        lo, hi = d.azimuth_limits()
        ring = d.survey_ring(40.0, 12, prefer="OBSERVE")
        self.assertEqual(len(ring), 12)
        for az, j in ring:
            with self.subTest(az=az):
                self.assertGreaterEqual(az, lo - 1e-6)
                self.assertLessEqual(az, hi + 1e-6)
                self.assertGreaterEqual(j["base_yaw"],
                                        LIMITS["base_yaw"][0] - 1e-6)
                self.assertLessEqual(j["base_yaw"],
                                     LIMITS["base_yaw"][1] + 1e-6)

    def test_asking_for_an_unreachable_azimuth_raises(self):
        """A clamped pose that looks somewhere else and claims to be the
        answer is the failure roommap.look_at refuses."""
        d = a_dome()
        _, hi = d.azimuth_limits()
        with self.assertRaises(dome_mod.OffDome) as cm:
            d.joints_for(hi + 30.0, 40.0)
        self.assertIn("behind the arm", str(cm.exception))


class AgainstTheRealConfig(unittest.TestCase):
    def test_it_builds_from_the_taught_poses_on_disk(self):
        """If config/poses.toml is retaught, this is what notices."""
        cfg = poses_mod.load()
        d = dome_mod.Dome(
            poses_mod.POSES,
            limits={j: cfg.soft_limits(j)
                    for j in ("base_yaw", "shoulder", "elbow", "wrist")})
        self.assertAlmostEqual(d.elevation_of(poses_mod.POSES["TALL"]), 0.0,
                               places=9)
        for name in ("OBSERVE", "ATTENTIVE", "HOME", "LOW"):
            with self.subTest(name):
                self.assertGreater(d.elevation_of(poses_mod.POSES[name]), 0.0)


class ItReportsHowWellItFitted(unittest.TestCase):
    """A gain without a residual is indistinguishable from noise.

    Two samples per joint is a fit through two points: it cannot fail and it
    cannot be checked. On the real arm that produced base_yaw gains of 0.21,
    0.24, 0.51 and 0.68 on four runs -- a 3x spread read out as four
    measurements, every one of them looking clean.
    """

    def samples(self, gains, mags=(2.0, 4.0, 6.0), jitter=0.0, seed=0,
                frame=(1280, 720), fov=(52.0, 31.0)):
        import random
        rng = random.Random(seed)
        w, h = frame
        out = []
        for joint in ("base_yaw", "shoulder", "elbow", "wrist"):
            for m in mags:
                for step in (+m, -m):
                    before = dict(POSES["HOME"])
                    after = dict(before, **{joint: before[joint] + step})
                    if joint == "base_yaw":
                        dx = -(gains["base_yaw"] * step / fov[0]) * w
                        dy = 0.0
                    else:
                        dx = 0.0
                        dy = (dome_mod.PITCH_SIGNS[joint] * gains[joint]
                              * step / fov[1]) * h
                    if jitter:
                        dx += rng.gauss(0, jitter)
                        dy += rng.gauss(0, jitter)
                    out.append((before, after, dx, dy))
        return out

    def test_clean_data_fits_and_says_so(self):
        truth = dict(dome_mod.DEFAULT_GAINS)
        d = a_dome()
        d.learn(self.samples(truth))
        self.assertTrue(d.fit["base_yaw"]["trustworthy"])
        self.assertTrue(d.fit["pitch"]["trustworthy"])
        self.assertGreater(d.fit["base_yaw"]["r2"], 0.99)

    def test_two_points_per_joint_is_refused_outright(self):
        """The structural flaw, and the real bug behind base_yaw's 3x spread.

        The guard was `len(rows_az) >= min_samples`, but rows_az held every
        sample while the fit used only the two that moved base_yaw -- so a
        two-point fit passed a three-sample check. Counting only the samples
        that moved the joint makes it refuse instead.
        """
        d = a_dome()
        got = d.learn(self.samples(dome_mod.DEFAULT_GAINS, mags=(2.0,)))
        self.assertNotIn("base_yaw", got)
        self.assertNotIn("base_yaw", d.fit)

    def test_a_base_yaw_sample_does_not_pollute_the_pitch_fit(self):
        """Its pitch row is all zeros, so it can never be explained -- and on
        real hardware its dy is not zero either."""
        d = a_dome()
        d.learn(self.samples(dome_mod.DEFAULT_GAINS))
        # 3 pitch joints x 3 magnitudes x 2 signs, with base_yaw's 6 excluded.
        self.assertEqual(d.fit["pitch"]["n"], 18)

    def test_noise_shows_up_in_the_residual(self):
        """The whole point: a gain fitted through scatter must look different
        from one fitted through a line."""
        d = a_dome()
        d.learn(self.samples(dome_mod.DEFAULT_GAINS, jitter=90.0, seed=5))
        self.assertFalse(d.fit["base_yaw"]["trustworthy"])
        self.assertGreater(d.fit["base_yaw"]["rms_deg"], 1.0)

    def test_more_magnitudes_give_more_samples_to_judge(self):
        d = a_dome()
        d.learn(self.samples(dome_mod.DEFAULT_GAINS, mags=(2.0, 4.0, 6.0)))
        self.assertEqual(d.fit["base_yaw"]["n"], 6)
        self.assertEqual(d.fit["pitch"]["n"], 18)
        self.assertTrue(d.fit["base_yaw"]["trustworthy"])


class ItDiagnosesWhyAFitIsPoor(unittest.TestCase):
    """An r2 says a fit is bad. It does not say what to do about it.

    Three causes look identical in an r2: noise scatters residuals randomly,
    a nonlinearity bends them with the size of the move, and BACKLASH splits
    them by direction of travel. Only the last is fixable by approaching every
    target from one side, and the base joint carries the most load, so it is
    the likeliest to have it.
    """

    FOV = (52.0, 31.0)
    FRAME = (1280, 720)

    def run_with(self, corrupt, joint="base_yaw", gain=0.62):
        d = a_dome()
        w, h = self.FRAME
        samples = []
        for m in (2.0, 4.0, 6.0, 8.0):
            for step in (+m, -m):
                before = dict(POSES["HOME"])
                after = dict(before, **{joint: before[joint] + step})
                dx = -(gain * step / self.FOV[0]) * w
                samples.append((before, after, dx + corrupt(step), 0.0))
        d.learn(samples, frame=self.FRAME, fov=self.FOV, min_samples=3)
        return d.diagnose(joint)

    def test_backlash_is_named(self):
        """A fixed offset whose sign follows the direction of travel."""
        got = self.run_with(lambda s: 25.0 if s > 0 else -25.0)
        self.assertIn("BACKLASH", got["verdict"])
        self.assertGreater(got["backlash_explains"], 0.2)

    def test_a_nonlinearity_is_named(self):
        """An error that grows with the size of the move."""
        got = self.run_with(lambda s: 0.9 * s * abs(s))
        self.assertIn("NONLINEAR", got["verdict"])
        self.assertGreater(got["nonlinearity_explains"], 0.2)

    def test_plain_noise_is_named(self):
        import random
        rng = random.Random(7)
        got = self.run_with(lambda s: rng.gauss(0, 18.0))
        self.assertIn("NOISE", got["verdict"])

    def test_a_clean_joint_is_called_clean(self):
        got = self.run_with(lambda s: 0.0)
        self.assertIn("CLEAN", got["verdict"])
        self.assertLess(got["unexplained_by_linear"], 0.01)

    def test_backlash_hides_inside_the_gain_if_you_only_look_at_residuals(self):
        """Why the first version of this failed its own test: with no extra
        term, a direction-dependent offset is absorbed into b, so the residual
        grouping it creates is largely cancelled."""
        d = a_dome()
        w, h = self.FRAME
        ss = [s for m in (2.0, 4.0, 6.0, 8.0) for s in (+m, -m)]
        k = 0.62 * w / self.FOV[0]
        ys = [-k * s + (25.0 if s > 0 else -25.0) for s in ss]
        lin = d._rss([[s] for s in ss], ys)
        back = d._rss([[s, 1.0 if s > 0 else -1.0] for s in ss], ys)
        self.assertLess(back, lin * 1e-6, "the offset term should fit exactly")

    def test_it_refuses_to_diagnose_too_little_data(self):
        d = a_dome()
        d.residuals = [{"joint": "base_yaw", "delta": 2.0, "dx": 1.0,
                        "predicted_px": 1.0}]
        self.assertIn("too few", d.diagnose("base_yaw")["verdict"])

    def test_one_direction_only_cannot_show_backlash(self):
        """Backlash is invisible if you never come back the other way."""
        d = a_dome()
        d.residuals = [{"joint": "base_yaw", "delta": m, "dx": -10.0 * m,
                        "predicted_px": -10.0 * m}
                       for m in (2.0, 4.0, 6.0, 8.0)]
        self.assertIn("one direction", d.diagnose("base_yaw")["verdict"])
