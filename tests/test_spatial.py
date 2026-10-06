"""Desk positions into words, from AVI's point of view."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import spatial as sp                     # noqa: E402

# Roughly today's desk, in lens heights: pens near AVI on its left, the
# laptop farther out and a little right, the mug beside it.
DESK = [{"label": "pen", "x": 2.0, "y": -1.8},
        {"label": "laptop", "x": 5.0, "y": 0.6},
        {"label": "mug", "x": 5.1, "y": 1.6}]


class Relations(unittest.TestCase):
    def test_left_and_right_are_avis(self):
        r = sp.relation({"label": "a", "x": 3, "y": -2},
                        {"label": "b", "x": 3, "y": 0})
        self.assertEqual(r["words"], "left of the b")

    def test_behind_means_farther_from_avi(self):
        r = sp.relation({"label": "a", "x": 5, "y": 0},
                        {"label": "b", "x": 2, "y": 0})
        self.assertEqual(r["words"], "behind the b")

    def test_diagonal_names_both(self):
        r = sp.relation({"label": "a", "x": 2, "y": -2},
                        {"label": "b", "x": 4, "y": 0})
        self.assertEqual(r["words"], "in front of and left of the b")

    def test_close_things_are_next_to_each_other(self):
        r = sp.relation({"label": "a", "x": 2.0, "y": 0.1},
                        {"label": "b", "x": 2.1, "y": 0.0})
        self.assertEqual(r["words"], "next to the b")


class Sentences(unittest.TestCase):
    def test_the_laptop_is_the_first_landmark(self):
        self.assertEqual(sp.describe("pen", DESK),
                         "In front of and left of the laptop, nearer to me.")

    def test_the_mug_beside_the_laptop(self):
        self.assertEqual(sp.describe("mug", DESK),
                         "Right of the laptop, farther from me.")

    def test_unknown_object_has_no_sentence(self):
        self.assertIsNone(sp.describe("wallet", DESK))

    def test_layout_is_nearest_first(self):
        self.assertEqual([r["label"] for r in sp.layout(DESK)],
                         ["pen", "laptop", "mug"])


class Units(unittest.TestCase):
    def test_metres_need_the_measured_height(self):
        with self.assertRaises(ValueError):
            sp.metres(2.0, 0.0)
        self.assertAlmostEqual(sp.metres(2.0, 0.25), 0.5)


if __name__ == "__main__":
    unittest.main()


class TwoOfAKind(unittest.TestCase):
    def test_each_object_is_described_as_itself(self):
        """Two backpacks: looking the second up by label described the
        first ("right of the laptop" for the one on the left)."""
        desk = [{"label": "laptop", "x": 3.6, "y": 0.5},
                {"label": "backpack", "x": 3.7, "y": 1.4, "hits": 11},
                {"label": "backpack", "x": 2.1, "y": -1.6, "hits": 5}]
        where = {r["y"]: r["where"] for r in sp.layout(desk)}
        self.assertIn("left of the laptop", where[-1.6])
        self.assertIn("ight of the laptop", where[1.4])

    def test_by_label_picks_the_one_seen_most(self):
        desk = [{"label": "laptop", "x": 3.6, "y": 0.5},
                {"label": "backpack", "x": 3.7, "y": 1.4, "hits": 11},
                {"label": "backpack", "x": 2.1, "y": -1.6, "hits": 5}]
        self.assertIn("ight of", sp.describe("backpack", desk))
