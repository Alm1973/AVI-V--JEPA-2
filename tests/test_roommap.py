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
        cy = H / 2 + (wrist - from_wrist) / WRIST_DPF * H
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

    def test_above_centre_means_a_lower_wrist(self):
        """Raising the wrist moves a landmark DOWN, so something high in frame
        lies at a lower wrist."""
        m, _ = fresh()
        _, wrist = m.direction_of(W / 2, 0, 90.0, 140.0)
        self.assertLess(wrist, 140.0)

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
        clock.tick(rm.HALF_LIFE_S * 6)
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
        clock.tick(rm.HALF_LIFE_S * 8)
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
