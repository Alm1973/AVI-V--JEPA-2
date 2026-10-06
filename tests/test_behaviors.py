"""Idle behaviour selection, including M2's own acceptance test.

BUILD_SPEC 15 says M2 is done when a 30-minute unattended run goes by without
the same behaviour sequence repeating three times in a row. That is the test at
the bottom, and it runs in milliseconds because nothing here touches a servo.
"""
import random
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.motion import gestures                      # noqa: E402
from avi_bot.personality import behaviors, mood as mood_mod   # noqa: E402


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make(hour=15, present=True, seed=7):
    c = Clock()
    m = mood_mod.Mood(clock=c, hour_fn=lambda: (hour, 0))
    m.present = present
    sel = behaviors.BehaviourSelector(rng=random.Random(seed), clock=c)
    return m, sel, c


class TestWiring(unittest.TestCase):
    def test_every_behaviour_the_spec_lists_exists(self):
        for name in ("still", "glance_at_you", "look_around",
                     "inspect_target", "fidget", "doze", "greet",
                     "sulk_droop", "celebrate"):
            self.assertIn(name, behaviors.BEHAVIOURS, name)

    def test_every_behaviour_has_a_weight_for_every_emotion(self):
        """A missing emotion would silently score 0, so a behaviour would
        vanish in one mood for no stated reason."""
        for name, spec in behaviors.BEHAVIOURS.items():
            missing = set(mood_mod.STYLES) - set(spec["weights"])
            self.assertEqual(missing, set(), f"{name} has no weight for {missing}")

    def test_every_named_gesture_actually_exists(self):
        for name, g in behaviors.GESTURE.items():
            if g is not None:
                self.assertIn(g, gestures.GESTURES, f"{name} -> {g}")

    def test_every_behaviour_has_a_gesture_entry(self):
        for name in behaviors.BEHAVIOURS:
            self.assertIn(name, behaviors.GESTURE, name)


class TestGates(unittest.TestCase):
    def test_nothing_that_needs_him_is_offered_when_he_is_away(self):
        m, sel, _ = make(present=False)
        us = sel.utilities(m, 0.0)
        self.assertNotIn("glance_at_you", us)
        self.assertNotIn("greet", us)
        for name in us:
            self.assertIn(name, behaviors.AWAY_ALLOWED, name)

    def test_inspect_needs_something_to_inspect(self):
        m, sel, _ = make()
        self.assertNotIn("inspect_target", sel.utilities(m, 0.0))
        m.event("new_object", target="mug")
        self.assertIn("inspect_target", sel.utilities(m, 0.0))

    def test_focus_leaves_almost_nothing_to_do(self):
        """Asking for focus means the robot stops being interesting."""
        m, sel, _ = make()
        m.event("focus_start")
        us = sel.utilities(m, 0.0)
        self.assertTrue(set(us) <= set(behaviors.FOCUS_ALLOWED), us)

    def test_focus_is_overwhelmingly_still(self):
        m, sel, c = make()
        m.event("focus_start")
        picks = Counter()
        for _ in range(400):
            c.advance(4.0)
            picks[sel.choose(m, c.t)] += 1
        self.assertGreater(picks["still"] / 400.0, 0.85,
                           f"too busy during focus: {picks}")

    def test_quiet_hours_allow_only_stillness_and_dozing(self):
        m, sel, _ = make(hour=23)
        us = sel.utilities(m, 0.0, quiet_hours=True)
        self.assertTrue(set(us) <= set(behaviors.QUIET_ALLOWED), us)

    def test_sulk_droop_is_impossible_when_not_sulking(self):
        """A weight of zero has to stay impossible, not merely unlikely --
        which is why the softmax runs on the logs."""
        m, sel, c = make()
        m.valence, m.arousal = 0.5, 0.5     # happy
        for _ in range(300):
            c.advance(4.0)
            self.assertNotEqual(sel.choose(m, c.t), "sulk_droop")


class TestCooldown(unittest.TestCase):
    def test_a_behaviour_is_unavailable_right_after_it_runs(self):
        m, sel, _ = make()
        sel.last_run["fidget"] = 100.0
        self.assertEqual(sel.cooldown_factor("fidget", 100.0), 0.0)
        self.assertNotIn("fidget", sel.utilities(m, 100.0))

    def test_it_ramps_back_rather_than_switching_on(self):
        m, sel, _ = make()
        cd = behaviors.BEHAVIOURS["fidget"]["cooldown"]
        sel.last_run["fidget"] = 0.0
        half = sel.cooldown_factor("fidget", cd / 2)
        self.assertAlmostEqual(half, 0.5, places=3)
        self.assertAlmostEqual(sel.cooldown_factor("fidget", cd * 2), 1.0)

    def test_still_has_no_cooldown(self):
        # Stillness following stillness is the resting state.
        self.assertEqual(behaviors.BEHAVIOURS["still"]["cooldown"], 0.0)


class TestMoodShapesChoice(unittest.TestCase):
    def run_many(self, m, sel, c, n=600):
        picks = Counter()
        for _ in range(n):
            c.advance(4.0)
            m.tick(c.t)
            picks[sel.choose(m, c.t)] += 1
        return picks

    def test_sleepy_mostly_dozes_or_sits_still(self):
        m, sel, c = make(hour=2)
        picks = self.run_many(m, sel, c)
        quiet = picks["doze"] + picks["still"]
        self.assertGreater(quiet / sum(picks.values()), 0.9, picks)

    def test_a_sulk_produces_drooping_and_no_celebrating(self):
        m, sel, c = make()
        picks = Counter()
        for _ in range(400):
            c.advance(4.0)
            m.sulk_until = c.t + 600      # held sulking
            picks[sel.choose(m, c.t)] += 1
        self.assertGreater(picks["sulk_droop"], 0, picks)
        self.assertEqual(picks["celebrate"], 0, picks)

    def test_curiosity_pulls_attention_to_the_target(self):
        m, sel, c = make()
        picks = Counter()
        for _ in range(400):
            c.advance(4.0)
            m.event("new_object", target="mug")   # kept alive
            picks[sel.choose(m, c.t)] += 1
        self.assertGreater(picks["inspect_target"], 0, picks)

    def test_stillness_dominates_even_when_happy(self):
        """Reachy Mini rests about 60% of the time, and a desk robot that
        fidgets continuously is furniture you unplug."""
        m, sel, c = make()
        m.valence, m.arousal = 0.5, 0.5
        picks = self.run_many(m, sel, c)
        self.assertGreater(picks["still"] / sum(picks.values()), 0.45, picks)


class TestM2Acceptance(unittest.TestCase):
    """BUILD_SPEC 15: M2 is done when a 30-minute unattended run never repeats
    the same behaviour sequence three times in a row."""

    def thirty_minutes(self, seed, hour=15, present=True):
        m, sel, c = make(hour=hour, present=present, seed=seed)
        end = c.t + 30 * 60
        picks = Counter()
        while c.t < end:
            c.advance(sel.rng.uniform(behaviors.GAP_MIN, behaviors.GAP_MAX))
            m.tick(c.t)
            picks[sel.choose(m, c.t)] += 1
        return sel, picks

    def test_no_behaviour_repeats_three_times_in_a_row(self):
        for seed in range(12):
            sel, picks = self.thirty_minutes(seed)
            self.assertLess(sel.repeats_longest_run(), 3,
                            f"seed {seed} looped: {picks}")

    def test_the_run_is_actually_varied(self):
        """Passing the repeat test by only ever being still would be cheating."""
        for seed in range(6):
            sel, picks = self.thirty_minutes(seed)
            distinct = [n for n, k in picks.items() if n != "still" and k > 0]
            self.assertGreaterEqual(len(distinct), 3,
                                    f"seed {seed} only did {picks}")

    def test_it_also_holds_overnight_and_with_nobody_there(self):
        for seed in range(6):
            sel, picks = self.thirty_minutes(seed, hour=3, present=False)
            self.assertLess(sel.repeats_longest_run(), 3,
                            f"seed {seed} looped: {picks}")

    def test_the_gap_between_behaviours_is_the_spec_s_two_to_six_seconds(self):
        m, sel, c = make()
        gaps = []
        for _ in range(200):
            sel.choose(m, c.t)
            gaps.append(sel.next_due - c.t)
            c.advance(gaps[-1])
        self.assertGreaterEqual(min(gaps), behaviors.GAP_MIN)
        self.assertLessEqual(max(gaps), behaviors.GAP_MAX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
