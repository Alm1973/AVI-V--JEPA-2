"""Motor babbling and the self-model (perception/selfmodel.py), on
synthetic rows: the numbers the robot run will be read with."""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.motion import body as body_mod, poses             # noqa: E402
from avi_bot.perception import selfmodel                       # noqa: E402

CFG = poses.load()
SEAT = poses.get_pose("ATTENTIVE")


def rows_from(px_per_deg, trials, noise=0.0, seed=1):
    """What babbling would record if each joint slid the picture by
    px_per_deg[joint] = (dx, dy) per degree."""
    rng = random.Random(seed)
    out = []
    for j, d in trials:
        kx, ky = px_per_deg[j]
        out.append({"joint": j, "delta": d, "pose": dict(SEAT),
                    "orb": {"dx": kx * d + rng.gauss(0, noise),
                            "dy": ky * d + rng.gauss(0, noise)},
                    "jepa": None})
    return out


class Planning(unittest.TestCase):
    def test_every_joint_in_turn_inside_the_limits(self):
        trials = selfmodel.plan(SEAT, 16, 8.0, CFG.soft_limits,
                                body=body_mod.load(), seed=3)
        self.assertEqual(len(trials), 16)
        self.assertEqual({j for j, _ in trials}, set(poses.JOINTS))
        for j, d in trials:
            lo, hi = CFG.soft_limits(j)
            self.assertTrue(lo <= SEAT[j] + d <= hi, (j, d))
            self.assertTrue(3.0 - 1e-9 <= abs(d) <= 8.0 + 1e-9, (j, d))

    def test_a_joint_at_its_limit_moves_the_other_way(self):
        # ATTENTIVE's shoulder (29.5) is 4.5 above its 25 limit
        trials = selfmodel.plan(SEAT, 8, 8.0, CFG.soft_limits, seed=0)
        for j, d in trials:
            if j == "shoulder":
                self.assertGreater(d, 0)


class Learning(unittest.TestCase):
    TRUE = {"base_yaw": (-24.0, 1.0), "shoulder": (0.0, -30.0),
            "elbow": (1.0, -25.0), "wrist": (0.0, 27.0)}

    def setUp(self):
        self.trials = selfmodel.plan(SEAT, 24, 8.0, CFG.soft_limits,
                                     seed=5)

    def test_the_slopes_come_back(self):
        got = selfmodel.slopes(rows_from(self.TRUE, self.trials, noise=4))
        for j, (kx, ky) in self.TRUE.items():
            self.assertAlmostEqual(got[j]["dx"], kx, delta=2.0)
            self.assertAlmostEqual(got[j]["dy"], ky, delta=2.0)

    def test_a_world_like_the_model_scores_the_model_right(self):
        b = body_mod.load()
        world = {}
        for j in poses.JOINTS:
            to = dict(SEAT, **{j: SEAT[j] + 1.0})
            world[j] = selfmodel.predicted_px(b, SEAT, to)
        rep = selfmodel.compare(rows_from(world, self.trials, noise=3), b)
        for j in ("shoulder", "elbow"):
            row = rep["joints"][j]
            a, n = row["model"]["direction_right"].split("/")
            self.assertEqual(a, n, row)
            self.assertEqual(row["old_rule"]["direction_right"],
                             f"0/{n}", "the old rule predicts nothing")

    def test_a_wrong_sign_shows_up(self):
        b = body_mod.load()
        flipped = dict(self.TRUE, shoulder=(0.0, 30.0))   # model: -30
        rep = selfmodel.compare(rows_from(flipped, self.trials), b)
        self.assertTrue(rep["joints"]["shoulder"]["model"]
                        ["direction_right"].startswith("0/"))
        self.assertIn("learned 100%", rep["verdict"]["shoulder"])


if __name__ == "__main__":
    unittest.main()


class TheDailyCheck(unittest.TestCase):
    """Once a day, while he is away: does every joint still move?"""

    TRUE = Learning.TRUE

    def test_when_it_runs(self):
        due = selfmodel.check_due
        self.assertTrue(due("10-09", "10-08", False, 120, 10)[0])
        self.assertFalse(due("10-09", "10-09", False, 120, 10)[0])
        self.assertFalse(due("10-09", "10-08", True, 120, 10)[0])
        self.assertFalse(due("10-09", "10-08", False, 20, 10)[0])
        self.assertFalse(due("10-09", "10-08", False, 120, 20)[0])
        self.assertFalse(due("10-09", "10-08", False, 120, 10,
                             enabled=False)[0])

    def test_a_stuck_elbow_is_told(self):
        trials = selfmodel.plan(SEAT, 8, 6.0, CFG.soft_limits, seed=2)
        history = rows_from(self.TRUE, trials, noise=3)
        expect = selfmodel.expected(history)
        broken = dict(self.TRUE, elbow=(0.5, -1.0))
        today = rows_from(broken, [(j, 4.0 if k % 2 else -4.0) for k, j in
                                   enumerate(poses.JOINTS * 2)], noise=2)
        v = selfmodel.judge_check(today, expect)
        self.assertEqual([j for j in poses.JOINTS if not v[j]["ok"]],
                         ["elbow"])
        self.assertEqual(selfmodel.check_words(v),
                         "My elbow didn't move in today's self-check. A "
                         "servo may be loose or unpowered.")

    def test_all_well_says_nothing(self):
        trials = selfmodel.plan(SEAT, 8, 6.0, CFG.soft_limits, seed=2)
        expect = selfmodel.expected(rows_from(self.TRUE, trials))
        v = selfmodel.judge_check(rows_from(self.TRUE, trials, noise=5),
                                  expect)
        self.assertTrue(all(x["ok"] for x in v.values()))
        self.assertIsNone(selfmodel.check_words(v))

    def test_no_history_uses_the_body_model(self):
        b = body_mod.load()
        expect = selfmodel.expected([], body=b, pose=SEAT)
        self.assertEqual(set(expect), set(poses.JOINTS))
