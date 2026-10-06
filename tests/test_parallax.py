"""The parallax test, checked against the numbers that motivated it."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import parallax                      # noqa: E402
from avi_bot.perception.parallax import (StaticRejector,      # noqa: E402
                                         MIN_EXPECTED_PX,
                                         STATIC_FRACTION,
                                         MOVING_FRACTION)

W = 1280
YAW_PER_FRAME = 29.0        # measured; config/joints.toml [camera]


def fresh(t0=1000.0):
    return StaticRejector(YAW_PER_FRAME), t0


# --- the measured case -----------------------------------------------------
# 2026-10-05, from the tracker's own log: base_yaw swung across 40.4 -> 56.9
# degrees while a 0.56-0.72 confidence box sat at x = 1023..1072. The real
# detections of Shaurya in the same run were at 0.75-0.89.
HARDWARE = [(40.4, 1004), (49.9, 1000), (56.9, 1046), (56.2, 1044),
            (53.9, 1042), (47.4, 1048), (45.2, 1048), (55.0, 1066),
            (56.2, 1047), (55.4, 1033), (54.8, 1030), (56.3, 1028),
            (53.9, 1036), (53.0, 1023), (56.4, 1015), (43.4, 1033)]




class Parallax(unittest.TestCase):
    def test_learns_the_arms_own_hardware(self):
        r, t = fresh()
        for i, (yaw, cx) in enumerate(HARDWARE):
            r.observe(yaw, cx, W, t=t + i * 0.4)
        assert r.zones, f"never learned a static zone from {r.describe()}"
        assert r.is_static(1035, t=t + 10), r.describe()



    def test_rejects_it_after_learning(self):
        r, t = fresh()
        for i, (yaw, cx) in enumerate(HARDWARE):
            r.observe(yaw, cx, W, t=t + i * 0.4)
        assert not r.accept(1040, t=t + 10)
        assert r.rejected == 1



    def test_a_real_person_passes(self):
        """Someone standing still while the arm pans slides across the frame."""
        r, t = fresh()
        # Person fixed in the room at the yaw where they appear centred.
        def cx_of(yaw):
            return W / 2.0 + (yaw - 90.0) / YAW_PER_FRAME * W
        accepted = 0
        for i, yaw in enumerate([90.0, 95.0, 100.0, 95.0, 90.0, 85.0]):
            cx = cx_of(yaw)
            if r.accept(cx, t=t + i * 0.4):
                accepted += 1
            r.observe(yaw, cx, W, t=t + i * 0.4)
        assert accepted == 6, r.describe()
        assert not r.zones, r.describe()



    def test_small_pans_prove_nothing(self):
        """Under the minimum the shift is comparable to box jitter, so no verdict."""
        r, t = fresh()
        small = MIN_EXPECTED_PX / W * YAW_PER_FRAME * 0.5
        for i in range(8):
            r.observe(90.0 + (i % 2) * small, 1035, W, t=t + i * 0.4)
        assert not r.zones, (f"judged a {small:.1f} deg pan: {r.describe()}")
        assert r.accept(1035, t=t + 10)



    def test_moving_clears_a_learned_zone(self):
        """A person who walks into a static zone is not censored forever."""
        r, t = fresh()
        for i, (yaw, cx) in enumerate(HARDWARE):
            r.observe(yaw, cx, W, t=t + i * 0.4)
        assert r.is_static(1035, t=t + 10)
        # Now something at that position DOES move with the camera.
        t2 = t + 12
        r.observe(90.0, 1035, W, t=t2)
        r.observe(80.0, 1035 - 10.0 / YAW_PER_FRAME * W, W, t=t2 + 0.4)
        assert not r.is_static(1035, t=t2 + 1), r.describe()



    def test_zones_expire(self):
        r, t = fresh()
        for i, (yaw, cx) in enumerate(HARDWARE):
            r.observe(yaw, cx, W, t=t + i * 0.4)
        assert r.is_static(1035, t=t + 10)
        # The TTL runs from the evidence, not from the start of the run.
        learned = max(z.at for z in r.zones)
        assert not r.is_static(1035, t=learned + parallax.ZONE_TTL_S + 1)



    def test_one_marginal_pairing_cannot_censor_the_frame(self):
        """A zone needs CONFIRMATIONS pairings before it rejects anything.

        Measured at HOME on 2026-10-05: three zones were learned, but only
        x=1109 was strong (0 px of movement across a 30 deg pan, expected 458).
        The other two rested on a single 14 px and 45 px reading of an expected
        229 -- and they sat at x=555 and x=692, bracketing the x=618..639 where
        Shaurya actually sits. One weak pairing must not blacklist his seat.
        """
        r, t = fresh()
        # Exactly one pairing: two sightings at one spot across a real pan.
        r.observe(90.0, 600, W, t=t)
        r.observe(105.0, 614, W, t=t + 0.4)
        self.assertTrue(r.zones, "should have noticed it")
        self.assertFalse(r.zones[0].confirmed)
        self.assertTrue(r.accept(600, t=t + 1),
                        "rejected on a single pairing")
        # A second independent pairing confirms it.
        r.observe(90.0, 607, W, t=t + 1)
        self.assertTrue(r.zones[0].confirmed, r.describe())
        self.assertFalse(r.accept(600, t=t + 2))

    def test_rejection_radius_spares_where_he_sits(self):
        """Learning is generous, rejecting is not -- the errors are not
        symmetric. A zone at 555 must not reach a detection at 618."""
        from avi_bot.perception import parallax as px
        self.assertLess(px.REJECT_PX, px.SAME_PX)
        r, t = fresh()
        for i in range(3):
            r.observe(90.0, 555, W, t=t + i)
            r.observe(105.0, 550, W, t=t + i + 0.4)
        self.assertTrue(r.zones[0].confirmed)
        self.assertFalse(r.accept(555, t=t + 10), "should reject its own spot")
        self.assertTrue(r.accept(618, t=t + 10),
                        "must not reach the seat at 618")

    def test_a_drifting_box_is_not_rigidly_attached(self):
        """Pairs that drift are two objects, not one static one.

        At HOME on 2026-10-05 the real arm hardware was found at x = 1109,
        1109, 1109 -- zero drift across a 30 degree pan. The spurious zones in
        the same runs drifted 14, 36 and 45 px, and one of them landed at
        x = 657, close enough to Shaurya's seat at 618 to censor him. The ratio
        test passed all of them; an absolute ceiling does not.
        """
        from avi_bot.perception import parallax as px
        r, t = fresh()
        drift = px.MAX_STATIC_DRIFT_PX + 11      # the measured 36 px case
        for i in range(4):
            r.observe(90.0, 657, W, t=t + i)
            r.observe(105.0, 657 + drift, W, t=t + i + 0.4)
        self.assertFalse(r.zones, f"learned a drifting pair: {r.describe()}")
        self.assertTrue(r.accept(618, t=t + 10), "censored his seat")

    def test_zero_drift_is_still_learned(self):
        """The real case must survive the tightening: x=1109, 0 px of 458."""
        r, t = fresh()
        for i in range(3):
            r.observe(75.0, 1109, W, t=t + i * 2)
            r.observe(105.0, 1109, W, t=t + i * 2 + 0.4)
        self.assertTrue(r.zones, "lost the one case that was certain")
        self.assertTrue(r.zones[0].confirmed, r.describe())
        self.assertFalse(r.accept(1109, t=t + 20))

    def test_nothing_known_passes(self):
        """The default is to accept: a dropped frame is cheap, a lock-on is not."""
        r, t = fresh()
        assert r.accept(640, t=t)
        assert r.rejected == 0



    def test_expected_px_matches_the_measurement(self):
        """17 degrees of pan should move a real target about 750 px at 1280."""
        r, _ = fresh()
        assert 700 <= r.expected_px(16.5, W) <= 780, r.expected_px(16.5, W)



    def test_thresholds_leave_a_gap(self):
        """A candidate between the two fractions must not flip a zone either way."""
        assert STATIC_FRACTION < MOVING_FRACTION


if __name__ == "__main__":
    unittest.main()
