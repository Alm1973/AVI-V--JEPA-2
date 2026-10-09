"""Performances AVI makes up (motion/choreo.py), keeps (memory/db.py
learned_moves) and plays (main.Avi._perform). 'Do an interpretive dance
like a dolphin' -- Shaurya, 2026-10-09."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.brain import intents, tools                       # noqa: E402
from avi_bot.memory import db as memdb                         # noqa: E402
from avi_bot.motion import body as body_mod, choreo, poses     # noqa: E402
from tests.test_main import Clock, MotionArm, build             # noqa: E402

CFG = poses.load()
SEAT = poses.get_pose("ATTENTIVE")


def sample(move, n=400):
    """Offsets along the whole move."""
    return [move.sample(move.duration * k / n)[1] for k in range(n + 1)]


class Composing(unittest.TestCase):
    def test_the_vocabulary_matches_the_tool(self):
        self.assertEqual(tools.PERFORM_MOVES, choreo.NAMES)

    def test_every_primitive_starts_and_ends_where_it_was(self):
        for name in choreo.NAMES:
            move, _ = choreo.compose([{"move": name}], SEAT, config=CFG)
            first, last = sample(move)[0], sample(move)[-1]
            for j in poses.JOINTS:
                self.assertAlmostEqual(first[j], 0.0, 6, name)
                self.assertAlmostEqual(last[j], 0.0, 6, name)

    def test_it_never_leaves_the_soft_limits(self):
        for pose in ("ATTENTIVE", "TALL", "HOME", "LOW", "OBSERVE"):
            start = poses.get_pose(pose)
            for name in choreo.NAMES:
                move, _ = choreo.compose([{"move": name, "size": 3}], start,
                                         config=CFG)
                for off in sample(move, 100):
                    for j in poses.JOINTS:
                        lo, hi = CFG.soft_limits(j)
                        v = start[j] + off[j]
                        self.assertTrue(
                            min(lo, start[j]) - 1e-6 <= v
                            <= max(hi, start[j]) + 1e-6,
                            f"{pose} {name} {j} {v}")

    def test_it_is_never_faster_than_the_arm(self):
        moves, _ = choreo.improvise("happy party dance")
        move, _ = choreo.compose(moves, SEAT, config=CFG)
        self.assertLessEqual(move.peak_rate(), CFG.achievable + 1e-6)

    def test_a_joint_near_its_limit_is_shifted_not_flattened(self):
        """ATTENTIVE's shoulder is 2.5 degrees above its limit: scaling
        alone left a dolphin's arc 30% of its shoulder."""
        move, info = choreo.compose([{"move": "leap"}], SEAT, config=CFG)
        swing = [o["shoulder"] for o in sample(move)]
        self.assertGreater(max(swing) - min(swing), 15.0)
        self.assertTrue(any("shoulder +" in n for n in info["notes"]))

    def test_the_body_model_keeps_it_off_the_desk(self):
        b = body_mod.load()
        start = poses.get_pose("LOW")
        self.assertTrue(b.safe(start))
        move, info = choreo.compose([{"move": "duck", "size": 3}], start,
                                    config=CFG, body=b)
        for off in sample(move, 100):
            pose = {j: start[j] + off[j] for j in poses.JOINTS}
            self.assertTrue(b.safe(pose), pose)

    def test_unknown_moves_are_dropped_and_none_left_is_an_error(self):
        moves, dropped = choreo.clean([{"move": "moonwalk"}, "sway",
                                       {"move": "Look Left", "size": 9}])
        self.assertEqual([m["move"] for m in moves], ["sway", "look_left"])
        self.assertEqual(moves[1]["size"], 3)
        self.assertEqual(dropped, ["moonwalk"])
        with self.assertRaises(ValueError):
            choreo.compose([{"move": "moonwalk"}], SEAT, config=CFG)

    def test_it_is_capped_at_thirty_seconds(self):
        moves = [{"move": "spin", "times": 6, "speed": "slow"}] * 12
        move, info = choreo.compose(moves, SEAT, config=CFG)
        self.assertLessEqual(move.duration, choreo.MAX_SECONDS + 5)


class MakingOneUp(unittest.TestCase):
    def test_a_dolphin_leaps_and_swims(self):
        moves, idea = choreo.improvise("an interpretive dance like a dolphin")
        names = [m["move"] for m in moves]
        self.assertIn("leap", names)
        self.assertIn("wave", names)
        self.assertIn("dolphin", idea)

    def test_the_same_name_gets_the_same_dance(self):
        self.assertEqual(choreo.improvise("the macarena"),
                         choreo.improvise("the macarena"))

    def test_a_variant_is_a_different_dance(self):
        self.assertNotEqual(choreo.improvise("dolphin", 0)[0],
                            choreo.improvise("dolphin", 1)[0])


class Hearing(unittest.TestCase):
    def test_dance_requests(self):
        for text, name in (
                ("do an interpretive dance like a dolphin",
                 "an interpretive dance like a dolphin"),
                ("can you dance like a robot", "dance like a robot"),
                ("dance", "dance"), ("dance for me", "dance"),
                ("act like a chicken", "a chicken")):
            got = intents.match(text)
            self.assertEqual((got.tool, got.args), ("perform",
                                                    {"name": name}), text)

    def test_not_every_mention_of_dancing(self):
        for text in ("i was dancing yesterday", "what is a dance"):
            self.assertIsNone(intents.match(text), text)

    def test_the_tool_validates(self):
        ok, err = tools.validate_call("perform", {
            "name": "dolphin", "idea": "leaps",
            "moves": [{"move": "leap"}]}, lenient=True)
        self.assertIsNone(err)
        _, err = tools.validate_call("perform", {"name": "x",
                                                 "moves": "leap"})
        self.assertIn("not a list", err)


class Keeping(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = memdb.Store(clock=self.clock)
        self.addCleanup(self.store.close)

    def test_one_dance_by_any_wording(self):
        self.store.save_move("interpretive dance like a dolphin",
                             [{"move": "leap"}])
        self.assertIsNotNone(self.store.get_move("dolphin dance"))
        self.assertIsNotNone(self.store.get_move("do the dolphin dance"))
        self.assertIsNone(self.store.get_move("robot dance"))

    def test_unseen_grows_seen_stays(self):
        self.store.save_move("dolphin", [{"move": "leap"}])
        self.store.performed_move("dolphin", seen=False)
        self.assertAlmostEqual(self.store.get_move("dolphin")["scale"], 1.4)
        self.store.performed_move("dolphin", seen=True)
        self.store.performed_move("dolphin", seen=None)
        got = self.store.get_move("dolphin")
        self.assertAlmostEqual(got["scale"], 1.4)
        self.assertEqual(got["performed"], 3)
        for _ in range(5):
            self.store.performed_move("dolphin", seen=False)
        self.assertAlmostEqual(self.store.get_move("dolphin")["scale"], 2.0)

    def test_a_new_one_under_the_name_starts_over(self):
        self.store.save_move("dolphin", [{"move": "leap"}])
        self.store.rate_move("dolphin", -1)
        self.store.save_move("dolphin", [{"move": "wave"}])
        got = self.store.get_move("dolphin")
        self.assertEqual((got["score"], got["moves"]),
                         (0, [{"move": "wave"}]))


class PerformingArm(MotionArm):
    config = CFG

    def __init__(self, moved=True):
        super().__init__()
        self.pose = dict(SEAT)
        self.played, self.moved = [], moved

    def perform(self, move, label):
        self.played.append((label, move))
        return {"moved": self.moved, "back": True}

    def set_pose(self, pose):
        self.pose.update(pose)


class Performing(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.arm = PerformingArm()
        self.avi = build(clock=self.clock, arm=self.arm)
        self.addCleanup(self.avi.close)
        self.avi._sleep = lambda s: None
        self.avi.background = False

    def handle(self, text):
        return self.avi.handle(text)

    def test_offline_it_makes_one_up_and_keeps_it(self):
        r = self.avi.skills.run("perform", {
            "name": "an interpretive dance like a dolphin"})
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.data["made"], "improvised")
        self.assertIn("leap", r.data["moves"])
        self.assertEqual(len(self.arm.played), 1)
        kept = self.avi.store.get_move("dolphin dance")
        self.assertEqual(kept["performed"], 1)
        self.assertEqual(kept["seen"], 1)

    def test_the_second_time_it_is_remembered(self):
        self.avi.skills.run("perform", {"name": "robot dance"})
        r = self.avi.skills.run("perform", {"name": "do the robot dance"})
        self.assertEqual(r.data["made"], "remembered")

    def test_the_models_composition_is_played_and_kept(self):
        r = self.avi.skills.run("perform", {
            "name": "jellyfish", "idea": "drifts and pulses",
            "moves": [{"move": "circle", "speed": "slow"},
                      {"move": "bob", "times": 3}]})
        self.assertEqual((r.data["made"], r.data["moves"]),
                         ("composed", ["circle", "bob"]))
        self.assertEqual(self.avi.store.get_move("jellyfish")["idea"],
                         "drifts and pulses")

    def test_praise_right_after_keeps_it_scold_makes_a_new_one(self):
        self.avi.skills.run("perform", {"name": "dolphin"})
        first = self.avi.store.get_move("dolphin")["moves"]
        self.handle("good job avi")
        self.assertEqual(self.avi.store.get_move("dolphin")["score"], 1)
        self.handle("bad robot")
        self.handle("bad robot")
        self.assertEqual(self.avi.store.get_move("dolphin")["score"], -1)
        r = self.avi.skills.run("perform", {"name": "dolphin"})
        self.assertEqual(r.data["made"], "improvised")
        self.assertNotEqual(self.avi.store.get_move("dolphin")["moves"],
                            first)

    def test_praise_later_is_not_about_the_dance(self):
        self.avi.skills.run("perform", {"name": "dolphin"})
        self.clock.tick(600)
        self.handle("good job avi")
        self.assertEqual(self.avi.store.get_move("dolphin")["score"], 0)

    def test_nothing_seen_moving_means_bigger_next_time(self):
        self.arm.moved = False
        self.avi.skills.run("perform", {"name": "dolphin"})
        self.assertAlmostEqual(self.avi.store.get_move("dolphin")["scale"],
                               1.4)

    def test_from_rest_it_first_goes_where_it_can_dance(self):
        self.arm.pose = poses.get_pose("REST")
        self.avi.skills.run("perform", {"name": "dolphin"})
        self.assertEqual(self.arm.pose["shoulder"], SEAT["shoulder"])

    def test_the_model_is_told_what_it_made_up(self):
        self.avi.skills.run("perform", {"name": "dolphin"})
        self.handle("good job avi")
        self.assertEqual(self.avi._moves_words(), "dolphin (he liked it)")

    def test_offline_the_words_reach_the_dance(self):
        self.handle("do an interpretive dance like a dolphin")
        self.assertEqual(len(self.arm.played), 1)


if __name__ == "__main__":
    unittest.main()
