"""The spherical room map.

AVI is bolted to a desk, so the useful map is a direction per object, not a
position in metres. The geometry inverts the same two measured constants the
tracking loop uses, so a sign error here would point the arm at the wrong part
of the room while the sentence stayed correct -- which is exactly the failure
that cost a day on base_yaw.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import roommap as rm                  # noqa: E402

W, H = 1280, 720
YAW_DPF, WRIST_DPF = 83.9, 32.2


class Clock:
    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s
        return self.t


def fresh():
    clock = Clock()
    return rm.RoomMap(YAW_DPF, WRIST_DPF, (W, H), clock=clock), clock


def sight(m, label, yaw, wrist, conf=0.7, views=2, each=1):
    """Observe one real direction from `views` DIFFERENT poses.

    Confirmation needs independent viewpoints, so a test that sights from one
    pose is testing the thing the code now refuses to believe. The frame
    position is worked back from the pose so every view describes the same
    direction, which is what a real object would do.
    """
    for i in range(views):
        from_yaw = yaw - 12.0 * i
        from_wrist = wrist - 4.0 * i
        cx = W / 2 + (yaw - from_yaw) / YAW_DPF * W
        # MINUS: an object at a higher wrist than the camera's sits ABOVE
        # centre, because raising the wrist moves landmarks DOWN. This helper
        # generated synthetic sightings with the inverted sign, matching the
        # bug in direction_of, so the two agreed and every test built on it
        # passed while describing physics the arm does not have.
        cy = H / 2 - (wrist - from_wrist) / WRIST_DPF * H
        for _ in range(each):
            m.observe(label, cx, cy, from_yaw, from_wrist, conf=conf)
    return m


class Geometry(unittest.TestCase):
    def test_dead_centre_is_the_current_pose(self):
        m, _ = fresh()
        self.assertEqual(m.direction_of(W / 2, H / 2, 90.0, 140.0),
                         (90.0, 140.0))

    def test_right_of_centre_means_a_higher_yaw(self):
        """Raising yaw moves a landmark LEFT, so something on the right lies
        at a higher yaw. Same sign as attention.corrections()."""
        m, _ = fresh()
        yaw, _ = m.direction_of(W, H / 2, 90.0, 140.0)
        self.assertGreater(yaw, 90.0)
        self.assertAlmostEqual(yaw, 90.0 + YAW_DPF / 2, places=3)

    def test_above_centre_means_a_HIGHER_wrist(self):
        """Raising the wrist moves a landmark DOWN, so to bring something that
        is high in the frame down to the centre the wrist must go UP: it lies
        at a HIGHER wrist.

        This test used to be called test_above_centre_means_a_lower_wrist. Its
        premise was right and its conclusion inverted, and it pinned the bug
        it described in place for a day. Measured on the arm 2026-10-06: wrist
        151.3 / 157.3 / 163.3 put a laptop at cy 344 / 516 / 599.
        """
        m, _ = fresh()
        _, wrist = m.direction_of(W / 2, 0, 90.0, 140.0)
        self.assertGreater(wrist, 140.0)

    def test_below_centre_means_a_lower_wrist(self):
        m, _ = fresh()
        _, wrist = m.direction_of(W / 2, H, 90.0, 140.0)
        self.assertLess(wrist, 140.0)

    def test_the_measured_laptop_lands_where_it_was_centred(self):
        """The arm's own measurement, end to end. At wrist 157.3 the laptop sat
        at cy 516; lowering the wrist to ~150 centred it. With the measured
        slope of 21.3 px/deg the map must put it near 150, not 157 or 164."""
        m = rm.RoomMap(YAW_DPF, 720 / 21.25, (W, H), clock=Clock())
        _, wrist = m.direction_of(W / 2, 516, 90.0, 157.32)
        self.assertAlmostEqual(wrist, 150.0, delta=0.5)

    def test_the_wrist_round_trips_against_the_tracking_loop(self):
        """The yaw round trip below existed; the wrist one did not, which is
        exactly where the sign error lived. The tracking loop had it right."""
        from avi_bot.personality import attention
        from avi_bot.motion import poses
        cfg = poses.load()
        m = rm.RoomMap(cfg.camera["yaw_deg_per_frame"],
                       cfg.camera["wrist_deg_per_frame"], (W, H))
        a = attention.Attention(cfg)
        a.offer("face", W / 2, H * 0.85, W, H)
        a.choose()
        _, d_wrist = a.corrections()
        _, mapped = m.direction_of(W / 2, H * 0.85, 90.0, 140.0)
        self.assertLess(d_wrist, 0, "the loop lowers the wrist")
        self.assertLess(mapped, 140.0, "so the map must place it lower too")

    def test_it_round_trips_against_the_tracking_loop(self):
        """Where the map says to look must be where the loop would drive to."""
        from avi_bot.personality import attention
        from avi_bot.motion import poses
        cfg = poses.load()
        m = rm.RoomMap(cfg.camera["yaw_deg_per_frame"],
                       cfg.camera["wrist_deg_per_frame"], (W, H))
        a = attention.Attention(cfg)
        a.offer("face", W * 0.8, H / 2, W, H)
        a.choose()
        d_yaw, _ = a.corrections()
        mapped, _ = m.direction_of(W * 0.8, H / 2, 90.0, 140.0)
        # The loop pushes yaw the same way the map says the object lies.
        self.assertGreater(d_yaw, 0)
        self.assertGreater(mapped, 90.0)


class Building(unittest.TestCase):
    def test_one_viewpoint_is_not_enough(self):
        """The first survey took two frames per stop and everything came back
        'seen 2x', which looked like confirmation and was one picture twice."""
        m, _ = fresh()
        for _ in range(5):
            m.observe("mug", 900, 300, 90.0, 140.0, conf=0.6)
        self.assertIsNone(m.where_is("mug"))

    def test_two_viewpoints_confirm_it(self):
        m, _ = fresh()
        sight(m, "mug", 110.0, 142.0)
        got = m.where_is("mug")
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[0], 110.0, places=0)
        self.assertAlmostEqual(got[1], 142.0, places=0)

    def test_repeat_sightings_average(self):
        m, clock = fresh()
        for _ in range(5):
            m.observe("mug", 900, 300, 90.0, 140.0, conf=0.6)
            clock.tick(5)
        place = [p for p in m.places if p.label == "mug"][0]
        self.assertEqual(place.count, 5)

    def test_one_bad_box_does_not_drag_a_well_known_object(self):
        """Weighted mean: fifty sightings outvote one outlier."""
        m, clock = fresh()
        for _ in range(25):
            sight(m, "mug", 90.0, 140.0, conf=0.8)
            clock.tick(2)
        before = m.where_is("mug")[0]
        # An outlier just inside the same-object radius.
        m.observe("mug", 640 + int(0.1 * W), 360, 90.0, 140.0, conf=0.3)
        after = m.where_is("mug")[0]
        self.assertLess(abs(after - before), 0.5,
                        f"one box moved it {after - before:.2f} deg")

    def test_two_far_apart_sightings_are_two_objects(self):
        m, _ = fresh()
        m.observe("mug", 100, 300, 60.0, 140.0)
        m.observe("mug", 1100, 300, 120.0, 140.0)
        self.assertEqual(len([p for p in m.places if p.label == "mug"]), 2)

    def test_different_labels_never_merge(self):
        m, _ = fresh()
        m.observe("mug", 640, 360, 90.0, 140.0)
        m.observe("laptop", 640, 360, 90.0, 140.0)
        self.assertEqual(len(m.places), 2)


class Forgetting(unittest.TestCase):
    def test_a_sighting_decays(self):
        m, clock = fresh()
        sight(m, "pen", 90.0, 140.0)
        self.assertIsNotNone(m.where_is("pen"))
        clock.tick(rm.BASE_HALF_LIFE_S * 6)
        self.assertIsNone(m.where_is("pen"),
                          "still believed a day-old single sighting")

    def test_something_seen_constantly_survives(self):
        """A mug moves; a monitor does not. Rather than guess which, repeated
        sighting is what keeps a position alive."""
        m, clock = fresh()
        for _ in range(20):
            sight(m, "monitor", 90.0, 140.0)
            clock.tick(600)
        self.assertIsNotNone(m.where_is("monitor"))

    def test_forget_stale_shrinks_the_map(self):
        m, clock = fresh()
        m.observe("pen", 640, 360, 90.0, 140.0)
        clock.tick(rm.BASE_HALF_LIFE_S * 8)
        self.assertEqual(m.forget_stale(), 1)
        self.assertEqual(m.places, [])


class Anchors(unittest.TestCase):
    def test_a_direction_is_described_by_the_nearest_taught_pose(self):
        """'Near where TALL looks' is a sentence he can act on; '61.4 degrees
        of commanded yaw' is not."""
        m, _ = fresh()
        m.anchor("TALL", 90.0, 176.0)
        m.anchor("OBSERVE", 60.0, 147.9)
        name, gap = m.nearest_anchor(62.0, 150.0)
        self.assertEqual(name, "OBSERVE")
        self.assertLess(gap, 10.0)

    def test_no_anchors_is_not_an_error(self):
        m, _ = fresh()
        self.assertEqual(m.nearest_anchor(90.0, 140.0), (None, None))


class Surveying(unittest.TestCase):
    def test_stops_cover_the_range_with_overlap(self):
        m, _ = fresh()
        stops = m.survey_stops((20.0, 160.0), [140.0])
        yaws = [y for y, w in stops]
        self.assertGreaterEqual(len(stops), 2)
        # Consecutive stops must be closer than one frame, or anything on a
        # seam is never mapped at all.
        for a, b in zip(yaws, yaws[1:]):
            self.assertLess(abs(b - a), YAW_DPF, "left a seam")

    def test_several_wrist_levels_make_rows(self):
        m, _ = fresh()
        one = m.survey_stops((20.0, 160.0), [140.0])
        two = m.survey_stops((20.0, 160.0), [130.0, 150.0])
        self.assertEqual(len(two), 2 * len(one))

    def test_unexplored_shrinks_as_things_are_mapped(self):
        """This is what makes 'look around' purposeful: the gaze goes where
        the map is blank."""
        m, _ = fresh()
        limits, levels = (20.0, 160.0), [140.0]
        before = m.unexplored(limits, levels)
        self.assertTrue(before)
        for yaw, wrist in before[:2]:
            sight(m, "thing", yaw, wrist, conf=0.9)
        after = m.unexplored(limits, levels)
        self.assertLess(len(after), len(before))

    def test_a_fully_mapped_room_has_nothing_left_to_explore(self):
        m, _ = fresh()
        limits, levels = (80.0, 100.0), [140.0]
        for yaw, wrist in m.survey_stops(limits, levels):
            sight(m, "thing", yaw, wrist, conf=0.9)
        self.assertEqual(m.unexplored(limits, levels), [])


class Persistence(unittest.TestCase):
    def test_it_round_trips(self):
        m, clock = fresh()
        m.anchor("TALL", 90.0, 176.0)
        for _ in range(2):
            sight(m, "mug", 110.0, 142.0, conf=0.7)
            clock.tick(10)
        back = rm.RoomMap.from_dict(m.to_dict(), clock=clock)
        self.assertEqual(back.anchors["TALL"], (90.0, 176.0))
        a, b = m.where_is("mug"), back.where_is("mug")
        self.assertAlmostEqual(a[0], b[0], places=2)
        self.assertAlmostEqual(a[1], b[1], places=2)

    def test_the_dump_is_json_safe(self):
        import json
        m, _ = fresh()
        sight(m, "mug", 110.0, 142.0, conf=0.7)
        json.dumps(m.to_dict())

    def test_viewpoints_survive_a_round_trip(self):
        """Or a reloaded map would forget what it had confirmed."""
        m, clock = fresh()
        sight(m, "mug", 110.0, 142.0)
        back = rm.RoomMap.from_dict(m.to_dict(), clock=clock)
        self.assertTrue(back.places[0].confirmed)
        self.assertIsNotNone(back.where_is("mug"))


class Reachability(unittest.TestCase):
    """A direction outside the arm's reach is a true fact and a useless
    instruction. Clamping it makes the robot stare confidently at the wrong
    thing, so look_at refuses instead."""

    YAW_LIMITS, WRIST_LIMITS = (20.0, 160.0), (4.0, 176.0)

    def test_something_in_reach_is_returned(self):
        m, _ = fresh()
        sight(m, "mug", 110.0, 142.0)
        got = m.look_at("mug", self.YAW_LIMITS, self.WRIST_LIMITS)
        self.assertIsNotNone(got)

    def test_something_out_of_reach_raises(self):
        """The first real survey produced wrist 182.9 and yaw 16.3, both
        outside the limits -- objects the arm genuinely cannot face."""
        m, _ = fresh()
        sight(m, "book", 16.3, 182.9)
        with self.assertRaises(rm.Unreachable):
            m.look_at("book", self.YAW_LIMITS, self.WRIST_LIMITS)

    def test_an_unknown_label_is_none_not_an_exception(self):
        m, _ = fresh()
        self.assertIsNone(
            m.look_at("ocarina", self.YAW_LIMITS, self.WRIST_LIMITS))

    def test_reachable_is_inclusive_of_the_limits(self):
        m, _ = fresh()
        self.assertTrue(m.reachable(20.0, 4.0, self.YAW_LIMITS,
                                    self.WRIST_LIMITS))
        self.assertTrue(m.reachable(160.0, 176.0, self.YAW_LIMITS,
                                    self.WRIST_LIMITS))
        self.assertFalse(m.reachable(19.9, 90.0, self.YAW_LIMITS,
                                     self.WRIST_LIMITS))



class ALookThatFoundItElsewhere(unittest.TestCase):
    """The mug was 15 degrees off its place on every look; averaging moved
    a weight-17 place under a degree each time (robot, 2026-10-08)."""

    def setUp(self):
        self.m, self.clock = fresh()
        for _ in range(8):
            sight(self.m, "mug", 104.5, 144.5)
            self.clock.tick(3600)
        self.place = self.m.where_is("mug")

    def test_the_place_moves_to_where_it_was_seen(self):
        yaw0, wrist0, w0 = self.place
        # Seen dead centre from 15 degrees left of the old aim.
        self.m.correct("mug", W / 2, H / 2, yaw0 - 15.0, wrist0,
                       was=(yaw0, wrist0), conf=0.4)
        yaw, wrist, w = self.m.where_is("mug")
        self.assertAlmostEqual(yaw, yaw0 - 15.0, places=3)
        self.assertAlmostEqual(wrist, wrist0, places=3)
        self.assertGreaterEqual(w, w0 * 0.99, "same object, same belief")
        self.assertEqual(len([p for p in self.m.places
                              if p.label == "mug"]), 1)

    def test_a_place_already_there_is_merged_not_duplicated(self):
        yaw0, wrist0, _ = self.place
        sight(self.m, "mug", yaw0 - 15.0, wrist0)       # a second, weak place
        self.assertEqual(len([p for p in self.m.places
                              if p.label == "mug"]), 2)
        self.m.correct("mug", W / 2, H / 2, yaw0 - 15.0, wrist0,
                       was=(yaw0, wrist0))
        mugs = [p for p in self.m.places if p.label == "mug"]
        self.assertEqual(len(mugs), 1)
        self.assertAlmostEqual(mugs[0].yaw, yaw0 - 15.0, places=3)

    def test_with_no_place_at_the_aim_it_is_an_ordinary_sighting(self):
        p = self.m.correct("pen", W / 2, H / 2, 60.0, 130.0,
                           was=(70.0, 130.0))
        self.assertEqual((p.label, p.count), ("pen", 1))

    def test_the_wrong_shape_is_refused(self):
        self.m.shoulder, self.m.elbow = 78.4, 88.0
        yaw0, wrist0, _ = self.place
        with self.assertRaises(rm.Unreachable):
            self.m.correct("mug", W / 2, H / 2, yaw0 - 15, wrist0,
                           was=(yaw0, wrist0), shoulder=29.5, elbow=131.0)

if __name__ == "__main__":
    unittest.main()


class ArmConfiguration(unittest.TestCase):
    """The camera rides three links, so where it POINTS depends on shoulder
    and elbow too. The same (yaw, wrist) aims somewhere different if the
    shoulder has moved, so a map built at one shape cannot be extended at
    another -- and surveying across the taught poses would have violated this
    silently, since each pose sets all four joints.
    """

    def test_the_first_observation_fixes_the_shape(self):
        m, _ = fresh()
        m.observe("mug", 640, 360, 90.0, 140.0, shoulder=84.0, elbow=95.0)
        self.assertEqual((m.shoulder, m.elbow), (84.0, 95.0))

    def test_a_small_difference_is_tolerated(self):
        m, _ = fresh()
        m.observe("mug", 640, 360, 90.0, 140.0, shoulder=84.0, elbow=95.0)
        m.observe("mug", 640, 360, 95.0, 140.0, shoulder=85.0, elbow=96.0)
        self.assertEqual(len(m.places), 1)

    def test_a_different_shape_is_refused(self):
        m, _ = fresh()
        m.observe("mug", 640, 360, 90.0, 140.0, shoulder=84.0, elbow=95.0)
        with self.assertRaises(rm.Unreachable):
            m.observe("mug", 640, 360, 90.0, 140.0, shoulder=29.5, elbow=131.0)

    def test_not_being_told_is_allowed(self):
        """Callers that do not know the shoulder still work; they just get no
        protection from this particular mistake."""
        m, _ = fresh()
        m.observe("mug", 640, 360, 90.0, 140.0)
        self.assertIsNone(m.shoulder)
        self.assertTrue(m.valid_for(84.0, 95.0))

    def test_the_shape_survives_a_round_trip(self):
        m, clock = fresh()
        m.observe("mug", 640, 360, 90.0, 140.0, shoulder=84.0, elbow=95.0)
        back = rm.RoomMap.from_dict(m.to_dict(), clock=clock)
        self.assertEqual((back.shoulder, back.elbow), (84.0, 95.0))
        self.assertFalse(back.valid_for(29.5, 131.0))


class EarnedDurability(unittest.TestCase):
    """Durability comes from revisiting over time, not from frame count.

    The bug these pin down was found by reading the clock: under one flat
    half-life a place's lifetime grows as log2(weight), so every doubling of
    the evidence bought a fixed six hours and nothing could ever become
    permanent. A survey ran one evening and the whole map had expired by the
    next morning, which is the opposite of a robot that grows.
    """

    def test_a_single_glimpse_fades_but_survives_the_night(self):
        """Two angles on one evening should still be there in the morning --
        that overnight loss is the bug -- and gone by the next evening."""
        m, clock = fresh()
        sight(m, "cup", 90.0, 140.0)
        clock.tick(10 * 3600)
        self.assertIsNotNone(m.where_is("cup"),
                             "forgot the room overnight, which is the bug")
        clock.tick(26 * 3600)
        self.assertIsNone(m.where_is("cup"),
                          "a thing seen once, a day and a half ago, is a guess")

    def test_staring_does_not_buy_permanence(self):
        """A thousand frames of a mug is a long stare, not a long acquaintance.

        This is the regression test for the real defect: weight counted frames,
        so ten minutes in view outscored thirty separate days.
        """
        m, clock = fresh()
        for _ in range(1000):
            m.observe("mug", 640, 360, 90.0, 140.0)
            clock.tick(1)
        place = m.places[0]
        self.assertEqual(place.visits, 1, "one sitting is one visit")
        self.assertEqual(place.half_life, rm.BASE_HALF_LIFE_S)
        clock.tick(7 * 24 * 3600)
        self.assertIsNone(m.where_is("mug"),
                          "a thousand frames should not outlive a week away")

    def test_separated_visits_harden_the_belief(self):
        m, clock = fresh()
        for _ in range(5):
            sight(m, "monitor", 90.0, 140.0)
            clock.tick(24 * 3600)
        place = m.places[0]
        self.assertEqual(place.visits, 5)
        # Five visits multiply the base four times over.
        self.assertAlmostEqual(place.half_life,
                               rm.BASE_HALF_LIFE_S * rm.SPACING ** 4, places=3)
        clock.tick(30 * 24 * 3600)
        self.assertIsNotNone(m.where_is("monitor"),
                             "furniture seen on five days survives a month away")

    def test_the_same_sitting_is_one_visit(self):
        m, clock = fresh()
        m.observe("pen", 640, 360, 90.0, 140.0)
        clock.tick(rm.VISIT_GAP_S - 60)
        m.observe("pen", 640, 360, 90.0, 140.0)
        self.assertEqual(m.places[0].visits, 1)
        clock.tick(rm.VISIT_GAP_S + 60)
        m.observe("pen", 640, 360, 90.0, 140.0)
        self.assertEqual(m.places[0].visits, 2)

    def test_half_life_is_capped(self):
        m, clock = fresh()
        for _ in range(40):
            m.observe("wall", 640, 360, 90.0, 140.0)
            clock.tick(rm.VISIT_GAP_S + 1)
        self.assertEqual(m.places[0].half_life, rm.MAX_HALF_LIFE_S)

    def test_visits_survive_a_restart(self):
        """The dashboard restarts. If visits reset to 1 on load, every place
        is demoted to a first glimpse and the earned durability is a lie."""
        m, clock = fresh()
        for _ in range(4):
            sight(m, "lamp", 60.0, 150.0)
            clock.tick(24 * 3600)
        before = m.places[0]
        back = rm.RoomMap.from_dict(m.to_dict(), clock=clock)
        self.assertEqual(back.places[0].visits, before.visits)
        self.assertEqual(back.places[0].half_life, before.half_life)


class WeightIsBounded(unittest.TestCase):
    def test_weight_stops_climbing(self):
        m, _ = fresh()
        for _ in range(500):
            m.observe("mug", 640, 360, 90.0, 140.0)
        self.assertLessEqual(m.places[0].weight, rm.MAX_WEIGHT)

    def test_a_thing_that_moves_can_be_seen_to_move(self):
        """With an uncapped average, a thousand stale frames outvote every new
        sighting and the mug is believed to be where it no longer is."""
        m, _ = fresh()
        for _ in range(500):
            m.observe("mug", 640, 360, 90.0, 140.0)
        start = m.places[0].yaw
        # Same object, now a few degrees away -- inside SAME_DEG, so it
        # updates the existing place rather than making a new one.
        truth = m.direction_of(740, 360, 90.0, 140.0)[0]
        for _ in range(40):
            m.observe("mug", 740, 360, 90.0, 140.0)
        now = m.places[0].yaw
        self.assertGreater(abs(now - start), 3.0,
                           f"position barely budged ({now - start:.2f} deg)")
        self.assertLess(abs(now - truth), 1.5,
                        f"settled at {now:.2f}, the mug is at {truth:.2f}")


class BigThingsCannotBePinnedPrecisely(unittest.TestCase):
    """You cannot localise an object more precisely than its own extent.

    A fixed 10-degree threshold mapped ONE laptop as two confirmed places 20
    degrees apart in wrist. The cause is not a wrong constant -- changing
    wrist_deg_per_frame from 32.2 to 24.3 left the gap at 20.2 degrees. It is
    that a large box's centre sits near the middle of the frame across several
    tilt rows at once, so each row files the object at its own elevation and
    the map grows a copy per row. A point landmark slides out of frame; a
    laptop does not.
    """

    def test_a_small_object_keeps_the_tight_threshold(self):
        m, _ = fresh()
        self.assertEqual(m.same_deg(40), rm.SAME_DEG)
        self.assertEqual(m.same_deg(None), rm.SAME_DEG)

    def test_a_big_object_gets_a_threshold_its_own_width(self):
        m, _ = fresh()
        # 600 px of 1280 at 83.9 deg/frame is 39.3 degrees of view.
        self.assertAlmostEqual(m.same_deg(600), 600 / 1280 * YAW_DPF, places=3)
        self.assertGreater(m.same_deg(600), rm.SAME_DEG)

    def test_the_larger_of_the_two_sizes_decides(self):
        m, _ = fresh()
        self.assertEqual(m.same_deg(40, 600), m.same_deg(600))

    def test_one_laptop_across_two_tilt_rows_stays_one_place(self):
        """The regression, with the real numbers: rows 9.4 degrees of wrist
        apart, a laptop centred in both, previously two places."""
        m, _ = fresh()
        big = 620
        m.observe("laptop", 640, 360, 102.0, 157.2, conf=0.9, px_size=big)
        m.observe("laptop", 640, 360, 102.0, 147.8, conf=0.5, px_size=big)
        self.assertEqual(len(m.places), 1,
                         f"one laptop became {len(m.places)} places")

    def test_a_mug_beside_a_laptop_is_still_its_own_place(self):
        """The threshold must not grow so far that distinct objects merge.
        Different labels never merge anyway, so this checks two small ones."""
        m, _ = fresh()
        m.observe("mug", 300, 360, 90.0, 150.0, conf=0.8, px_size=70)
        m.observe("mug", 1000, 360, 90.0, 150.0, conf=0.8, px_size=70)
        self.assertEqual(len(m.places), 2)

    def test_two_separate_laptops_far_apart_still_separate(self):
        m, _ = fresh()
        m.observe("laptop", 640, 360, 40.0, 150.0, conf=0.9, px_size=500)
        m.observe("laptop", 640, 360, 140.0, 150.0, conf=0.9, px_size=500)
        self.assertEqual(len(m.places), 2)


class OneObjectOneName(unittest.TestCase):
    """The detector says "book" and "notebook" -- or "mug" and "cup" -- for the
    same thing from one frame to the next. Measured: one survey stop logged
    book:0.43, notebook:0.35, book:0.43, notebook for a single object."""

    def test_synonyms_land_in_one_place(self):
        m, _ = fresh()
        m.observe("notebook", 640, 360, 136.0, 158.7, conf=0.35)
        m.observe("book", 640, 360, 136.0, 158.7, conf=0.43)
        m.observe("cup", 640, 360, 90.0, 150.0, conf=0.3)
        m.observe("mug", 640, 360, 90.0, 150.0, conf=0.5)
        self.assertEqual(sorted(p.label for p in m.places), ["book", "mug"])

    def test_either_name_finds_it(self):
        m, _ = fresh()
        sight(m, "cup", 90.0, 150.0)
        self.assertIsNotNone(m.where_is("mug"))
        self.assertIsNotNone(m.where_is("cup"))

    def test_unrelated_labels_are_untouched(self):
        self.assertEqual(rm.canonical_label("  Laptop "), "laptop")
        self.assertEqual(rm.canonical_label("pen"), "pen")
