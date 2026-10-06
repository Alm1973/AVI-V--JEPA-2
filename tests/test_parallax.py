"""The parallax test, against the numbers measured on the real arm.

The case that matters most is the last one: the arm's own hardware and
Shaurya overlap in the image, which is what killed the first design, and the
replacement has to handle both at once without rejecting him.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import parallax as px                 # noqa: E402
from avi_bot.perception.parallax import (ParallaxTracker,      # noqa: E402
                                         REAL, ATTACHED, UNKNOWN)

W = 1280
DPF = 83.9                  # MEASURED; config/joints.toml [camera]
PX_PER_DEG = W / DPF        # 15.26


def fresh(t0=1000.0):
    return ParallaxTracker(DPF), t0


def det(cx, conf=0.8):
    return {"cx": float(cx), "conf": conf}


class Geometry(unittest.TestCase):
    def test_px_per_deg_matches_the_measurement(self):
        r, _ = fresh()
        self.assertAlmostEqual(r.px_per_deg(W), 15.26, places=1)

    def test_a_pan_must_be_big_enough_to_judge(self):
        """The two hypotheses have to separate before a match means anything."""
        need = px.MIN_SEPARATION_PX / PX_PER_DEG
        self.assertGreater(need, 5.0, "a 5 degree pan should prove nothing")
        self.assertLess(need, 15.0, "but 15 degrees must be enough to judge")


class Verdicts(unittest.TestCase):
    def test_arm_hardware_is_called_attached(self):
        """Measured at HOME: x = 1109, 1109, 1109 across a 30 degree pan that
        should have moved a real object 458 px."""
        r, t = fresh()
        for i, yaw in enumerate((75.0, 90.0, 105.0, 90.0)):
            out = r.classify([det(1109, 0.5)], yaw, W, t=t + i * 0.5)
        self.assertEqual(out[0]["parallax"], ATTACHED, r.describe())
        self.assertFalse(r.accept(out[0]))

    def test_a_real_object_is_called_real(self):
        """A fixed object in the room slides by d_yaw * px_per_deg."""
        r, t = fresh()
        cx, out = 640.0, None
        prev = 90.0
        for i, yaw in enumerate((90.0, 105.0, 90.0, 75.0)):
            cx -= (yaw - prev) * PX_PER_DEG
            prev = yaw
            out = r.classify([det(cx, 0.5)], yaw, W, t=t + i * 0.5)
        self.assertEqual(out[0]["parallax"], REAL, r.describe())
        self.assertTrue(r.accept(out[0]))

    def test_no_pan_means_no_verdict(self):
        r, t = fresh()
        out = None
        for i in range(5):
            out = r.classify([det(900, 0.5)], 90.0, W, t=t + i * 0.4)
        self.assertEqual(out[0]["parallax"], UNKNOWN, r.describe())

    def test_a_small_pan_still_means_no_verdict(self):
        r, t = fresh()
        out = None
        for i, yaw in enumerate((90.0, 93.0, 90.0, 93.0, 90.0)):
            out = r.classify([det(900, 0.5)], yaw, W, t=t + i * 0.4)
        self.assertEqual(out[0]["parallax"], UNKNOWN, r.describe())

    def test_one_pairing_is_not_enough(self):
        r, t = fresh()
        r.classify([det(1109, 0.5)], 75.0, W, t=t)
        out = r.classify([det(1109, 0.5)], 105.0, W, t=t + 0.5)
        self.assertEqual(out[0]["parallax"], UNKNOWN,
                         "acted on a single pairing")


class FallingBack(unittest.TestCase):
    def test_unknown_falls_back_to_confidence(self):
        """The arm is usually still, so UNKNOWN is the common case and
        refusing it would mean never seeing him while holding position."""
        r, _ = fresh()
        self.assertTrue(r.accept({"cx": 640, "conf": 0.80, "parallax": UNKNOWN}))
        self.assertFalse(r.accept({"cx": 640, "conf": 0.60, "parallax": UNKNOWN}))

    def test_real_beats_a_low_score(self):
        """Proven to be in the room, so confidence stops being the gate."""
        r, _ = fresh()
        self.assertTrue(r.accept({"cx": 640, "conf": 0.45, "parallax": REAL}))

    def test_attached_is_rejected_however_confident(self):
        r, _ = fresh()
        self.assertFalse(r.accept({"cx": 640, "conf": 0.99,
                                   "parallax": ATTACHED}))


class TheCaseThatKilledTheFirstDesign(unittest.TestCase):
    def test_he_is_kept_while_overlapping_hardware_is_rejected(self):
        """Hardware at x=632 and him at x=618-639 -- 14 px apart.

        The region-based version blacklisted x=632 with 8 pairings and would
        have thrown him away with it. Here the verdict belongs to a track, and
        the two tracks separate the moment the arm pans, because only one of
        them moves.
        """
        r, t = fresh()
        him = 625.0
        prev = 90.0
        out = None
        for i, yaw in enumerate((90.0, 105.0, 90.0, 75.0, 90.0)):
            him -= (yaw - prev) * PX_PER_DEG
            prev = yaw
            # Both in frame at once, starting 7 px apart.
            out = r.classify([det(him, 0.80), det(632, 0.55)],
                             yaw, W, t=t + i * 0.5)
        by_track = {d["cx"]: d["parallax"] for d in out}
        hardware = [p for cx, p in by_track.items() if abs(cx - 632) < 1]
        person = [p for cx, p in by_track.items() if abs(cx - 632) >= 1]
        self.assertEqual(hardware, [ATTACHED], r.describe())
        self.assertEqual(person, [REAL], r.describe())
        kept = [d for d in out if r.accept(d)]
        self.assertEqual(len(kept), 1)
        self.assertNotAlmostEqual(kept[0]["cx"], 632.0, places=0)

    def test_nothing_is_rejected_for_where_it_sits(self):
        """No position is ever blacklisted, so a fresh detection anywhere --
        including on top of known hardware -- starts with a clean slate."""
        r, t = fresh()
        for i, yaw in enumerate((75.0, 105.0, 75.0, 105.0)):
            r.classify([det(1109, 0.5)], yaw, W, t=t + i * 0.5)
        self.assertTrue(r.attached_tracks(), "should know about the hardware")
        # Same place, but after the track has expired: judged afresh. The TTL
        # runs from the LAST sighting, not from the start of the run.
        later = max(k.at for k in r.tracks) + px.TRACK_TTL_S + 1
        out = r.classify([det(1109, 0.85)], 90.0, W, t=later)
        self.assertEqual(out[0]["parallax"], UNKNOWN)
        self.assertTrue(r.accept(out[0]), "rejected on position alone")


class Housekeeping(unittest.TestCase):
    def test_tracks_expire(self):
        r, t = fresh()
        r.classify([det(500)], 90.0, W, t=t)
        self.assertTrue(r.tracks)
        r.classify([], 90.0, W, t=r.tracks[0].at + px.TRACK_TTL_S + 1)
        self.assertFalse(r.tracks)

    def test_two_detections_cannot_claim_one_track(self):
        r, t = fresh()
        r.classify([det(640)], 90.0, W, t=t)
        out = r.classify([det(645, 0.9), det(650, 0.8)], 90.0, W, t=t + 0.4)
        self.assertEqual(len({d["track"] for d in out}), 2)

    def test_empty_input_is_fine(self):
        r, t = fresh()
        self.assertEqual(r.classify([], 90.0, W, t=t), [])


if __name__ == "__main__":
    unittest.main()


class TrustFollowsTheLight(unittest.TestCase):
    """A fixed threshold is wrong somewhere.

    Measured 2026-10-06 across four poses in an empty room: the worst false
    'person' was 0.61-0.72 in the dark and 0.31 in daylight, while Shaurya
    reads 0.75-0.90 in both. So the overlap behind all of yesterday's trouble
    is substantially a low-light artifact, and one number cannot serve both.
    """

    def test_daylight_is_permissive(self):
        self.assertEqual(px.trust_for_light(125.0), 0.65)

    def test_darkness_is_strict(self):
        self.assertEqual(px.trust_for_light(25.0), 0.85)

    def test_it_is_monotonic(self):
        levels = [0, 10, 30, 49, 50, 79, 80, 120, 255]
        trusts = [px.trust_for_light(l) for l in levels]
        self.assertEqual(trusts, sorted(trusts, reverse=True),
                         f"not monotonic in light: {list(zip(levels, trusts))}")

    def test_unknown_brightness_gets_the_strictest(self):
        """An unmeasured frame is not evidence of a well-lit one."""
        self.assertEqual(px.trust_for_light(None),
                         max(px.trust_for_light(x) for x in (0, 50, 80, 200)))

    def test_the_measured_false_positives_are_excluded(self):
        """The numbers that motivated this: 0.72 in the dark, 0.31 in light."""
        self.assertGreater(px.trust_for_light(30.0), 0.72)
        self.assertGreater(px.trust_for_light(125.0), 0.31)

    def test_he_is_still_accepted_in_both(self):
        """0.75 is his worst measured reading; it must pass at every level."""
        for level in (25.0, 55.0, 125.0):
            self.assertLessEqual(px.trust_for_light(level), 0.85)
        # In daylight his typical 0.82 must clear the bar comfortably.
        self.assertLess(px.trust_for_light(125.0), 0.82)

    def test_a_dark_frame_would_have_rejected_last_nights_false_positive(self):
        r, t = fresh()
        d = {"cx": 632, "conf": 0.68, "parallax": UNKNOWN}
        self.assertFalse(r.accept(d, min_conf_unknown=px.trust_for_light(30.0)))
        self.assertTrue(r.accept(d, min_conf_unknown=px.trust_for_light(125.0)))
