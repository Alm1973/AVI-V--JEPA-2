"""The mood engine: BUILD_SPEC 5.2's table, and the label it implies.

Everything here runs on an injected clock, so a fifteen-minute half-life is
tested in microseconds and the result does not depend on the machine's speed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.motion import engine                       # noqa: E402
from avi_bot.personality import mood as mood_mod        # noqa: E402


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make(hour=15, minute=0):
    c = Clock()
    m = mood_mod.Mood(clock=c, hour_fn=lambda: (hour, minute))
    return m, c


class TestDecay(unittest.TestCase):
    def test_valence_halves_toward_baseline_over_its_half_life(self):
        m, c = make()
        base = mood_mod.BASELINE["valence"]
        m.valence = base + 0.8
        c.advance(mood_mod.HALF_LIFE["valence"])
        m.tick()
        self.assertAlmostEqual(m.valence, base + 0.4, places=4)

    def test_decay_does_not_depend_on_how_often_tick_is_called(self):
        """A fixed step per tick would make mood drift faster on a busy loop
        than a quiet one, which is a bug that only shows up in production."""
        slow, c1 = make()
        fast, c2 = make()
        slow.valence = fast.valence = 0.9
        c1.advance(600.0)
        slow.tick()
        for _ in range(600):
            c2.advance(1.0)
            fast.tick()
        self.assertAlmostEqual(slow.valence, fast.valence, places=6)

    def test_arousal_decays_five_times_faster_than_valence(self):
        self.assertAlmostEqual(
            mood_mod.HALF_LIFE["valence"] / mood_mod.HALF_LIFE["arousal"], 5.0)

    def test_values_stay_inside_their_ranges(self):
        m, _ = make()
        for _ in range(20):
            m.event("praise")
        self.assertLessEqual(m.valence, 1.0)
        self.assertLessEqual(m.arousal, 1.0)
        for _ in range(20):
            m.event("scold")
        self.assertGreaterEqual(m.valence, -1.0)


class TestEvents(unittest.TestCase):
    def test_praise_and_scold_move_valence_opposite_ways(self):
        m, _ = make()
        start = m.valence
        m.event("praise")
        self.assertGreater(m.valence, start)
        m2, _ = make()
        m2.event("scold")
        self.assertLess(m2.valence, start)

    def test_scold_starts_a_five_minute_sulk(self):
        m, c = make()
        m.event("scold")
        self.assertTrue(m.sulking())
        c.advance(mood_mod.SULK_S - 1)
        self.assertTrue(m.sulking())
        c.advance(2)
        self.assertFalse(m.sulking())

    def test_praise_ends_a_sulk_immediately(self):
        """The intended way out of a sulk, per the character sheet."""
        m, _ = make()
        m.event("scold")
        self.assertTrue(m.sulking())
        m.event("praise")
        self.assertFalse(m.sulking())

    def test_arriving_clears_the_wish_for_attention(self):
        m, _ = make()
        m.social = 0.9
        m.event("arrived")
        self.assertEqual(m.social, 0.0)
        self.assertTrue(m.present)

    def test_focus_forces_arousal_down_and_cannot_raise_it(self):
        m, _ = make()
        m.arousal = 0.05
        m.event("focus_start")
        self.assertLessEqual(m.arousal, 0.05)
        self.assertTrue(m.focus)

    def test_a_new_object_becomes_the_curiosity_target_and_expires(self):
        m, c = make()
        m.event("new_object", target="red mug")
        self.assertEqual(m.curiosity_target, "red mug")
        c.advance(mood_mod.CURIOSITY_S - 1)
        m.tick()
        self.assertEqual(m.curiosity_target, "red mug")
        c.advance(2)
        m.tick()
        self.assertIsNone(m.curiosity_target)

    def test_an_unknown_event_raises_rather_than_passing_silently(self):
        m, _ = make()
        with self.assertRaises(ValueError):
            m.event("vibes")


class TestBeingIgnored(unittest.TestCase):
    def test_social_only_rises_while_he_is_actually_there(self):
        """Wanting attention from an empty room is pointless, and it would
        leave the robot "bored" the moment he walked back in."""
        away, c = make()
        away.present = False
        c.advance(5 * 3600)
        away.tick()
        self.assertAlmostEqual(away.social, mood_mod.BASELINE["social"])

    def test_social_rises_in_steps_once_ignored_long_enough(self):
        m, c = make()
        m.event("arrived")            # resets social to 0
        c.advance(mood_mod.IGNORED_AFTER_S - 60)
        m.tick()
        self.assertEqual(m.social, 0.0, "rose before the 45 minute threshold")
        c.advance(120)
        m.tick()
        self.assertAlmostEqual(m.social, 0.1, places=6)
        c.advance(mood_mod.IGNORED_STEP_S + 1)
        m.tick()
        self.assertAlmostEqual(m.social, 0.2, places=6)

    def test_interacting_stops_it_climbing(self):
        m, c = make()
        m.event("arrived")
        c.advance(mood_mod.IGNORED_AFTER_S + 60)
        m.tick()
        before = m.social
        m.event("praise")             # an interaction
        c.advance(mood_mod.IGNORED_STEP_S + 1)
        m.tick()
        self.assertAlmostEqual(m.social, before, places=6)


class TestEnergy(unittest.TestCase):
    def test_energy_bottoms_out_in_quiet_hours(self):
        for hour in (22, 23, 0, 3, 6):
            m, _ = make(hour=hour)
            self.assertLess(m.energy, 0.3, f"{hour}:00 should be sleepy")

    def test_energy_peaks_during_his_desk_hours(self):
        m, _ = make(hour=15)
        self.assertGreater(m.energy, 0.8)

    def test_energy_is_continuous_across_the_day(self):
        """A step would flip behaviour the instant a clock minute rolled over."""
        vals = [mood_mod.energy_for_hour(h, mi)
                for h in range(7, 22) for mi in (0, 30)]
        jumps = [abs(b - a) for a, b in zip(vals, vals[1:])]
        self.assertLess(max(jumps), 0.1)


class TestEmotion(unittest.TestCase):
    def test_excited_needs_both_high_valence_and_high_arousal(self):
        m, _ = make()
        m.valence, m.arousal = 0.5, 0.7
        self.assertEqual(m.emotion(), "excited")
        m.arousal = 0.4
        self.assertEqual(m.emotion(), "happy")

    def test_sulky_beats_curious(self):
        """The documented departure from the spec's listed order: a sulk you
        can be distracted out of in one second is not a sulk."""
        m, _ = make()
        m.event("scold")
        m.event("new_object", target="mug")
        self.assertEqual(m.emotion(), "sulky")

    def test_sleepy_wins_when_there_is_no_energy(self):
        m, _ = make(hour=2)
        m.valence, m.arousal = 0.5, 0.7
        self.assertEqual(m.emotion(), "sleepy")

    def test_bored_needs_to_be_ignored_and_calm(self):
        m, _ = make()
        m.social, m.arousal, m.valence = 0.7, 0.2, 0.0
        self.assertEqual(m.emotion(), "bored")

    def test_every_emotion_label_has_a_motion_style(self):
        m, _ = make()
        labels = set()
        for hour in (3, 15):
            for val in (-0.5, 0.0, 0.5):
                for ar in (0.1, 0.5, 0.8):
                    mm, _ = make(hour=hour)
                    mm.valence, mm.arousal = val, ar
                    labels.add(mm.emotion())
        mm, _ = make()
        mm.event("scold")
        labels.add(mm.emotion())
        mm2, _ = make()
        mm2.event("new_object", target="x")
        labels.add(mm2.emotion())
        mm3, _ = make()
        mm3.social, mm3.arousal, mm3.valence = 0.7, 0.2, 0.0
        labels.add(mm3.emotion())
        for label in labels:
            self.assertIn(label, mood_mod.STYLES,
                          f"emotion {label!r} has no entry in STYLES")

    def test_snapshot_is_json_safe(self):
        import json
        m, _ = make()
        m.event("new_object", target="mug")
        json.dumps(m.snapshot())


class TestStyle(unittest.TestCase):
    def test_style_kwargs_build_a_real_engine_style(self):
        for label in mood_mod.STYLES:
            s = engine.Style(**mood_mod.style_for(label))
            self.assertGreater(s.speed, 0.0)
            self.assertGreater(s.amplitude, 0.0)

    def test_no_style_asks_for_more_speed_than_the_arm_has_headroom_for(self):
        """Gestures are already sized to the arm's measured ceiling, so a big
        speed multiplier is not refused -- it is silently clamped, which smears
        the gesture into a different shape."""
        for label, s in mood_mod.STYLES.items():
            self.assertLessEqual(s["speed"], 1.2, label)

    def test_happy_moves_faster_than_sad_and_bounces(self):
        self.assertGreater(mood_mod.STYLES["happy"]["speed"],
                           mood_mod.STYLES["sad"]["speed"])
        self.assertEqual(mood_mod.STYLES["happy"]["bounce"], 1.0)
        self.assertEqual(mood_mod.STYLES["sad"]["bounce"], 0.0)

    def test_bounce_actually_changes_the_easing_curve(self):
        """Otherwise "happy is bouncy" is a number nobody reads."""
        happy = engine.Style(**mood_mod.style_for("happy"))
        sad = engine.Style(**mood_mod.style_for("sad"))
        self.assertEqual(happy.ease_for("ease_in_out"), "ease_out_back")
        self.assertEqual(sad.ease_for("ease_in_out"), "ease_in_out")

    def test_sulking_is_delayed_as_well_as_slow(self):
        # "reluctant and delayed" (BUILD_SPEC 5.5); a Style cannot express
        # delay, so it is carried separately.
        self.assertGreater(mood_mod.start_delay_for("sulky"), 0.0)
        self.assertEqual(mood_mod.start_delay_for("happy"), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
