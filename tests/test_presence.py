"""Learning when Shaurya is at the desk.

The two rules that make it trustworthy are what these pin: absence needs a
view of his seat, and arrivals and departures need more than one frame.
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import presence as pm                  # noqa: E402

T0 = datetime(2026, 10, 6, 19, 0).timestamp()      # 7pm, a weekday evening


_OPEN = []


def log():
    p = pm.Presence(":memory:", clock=lambda: T0)
    _OPEN.append(p)
    return p


def tearDownModule():
    # An unclosed sqlite handle per test is a ResourceWarning per test, and
    # a wall of those hides real output.
    while _OPEN:
        _OPEN.pop().close()


def run(p, pattern, start=T0, step=20.0, view_ok=True):
    """Feed a string of 1/0 looks, one every `step` seconds."""
    events = []
    t = start
    for ch in pattern:
        e = p.sample(ch == "1", conf=0.8 if ch == "1" else None,
                     view_ok=view_ok, at=t)
        if e:
            events.append((e, t))
        t += step
    return events, t


class Arriving(unittest.TestCase):
    def test_one_frame_is_not_an_arrival(self):
        p = log()
        events, _ = run(p, "1000000")
        self.assertEqual(events, [])
        self.assertFalse(p.here)

    def test_consecutive_sightings_are(self):
        p = log()
        events, _ = run(p, "011")
        self.assertEqual([e for e, _ in events], ["arrived"])
        self.assertTrue(p.here)


class Leaving(unittest.TestCase):
    def test_a_missed_frame_does_not_end_a_session(self):
        p = log()
        events, _ = run(p, "11" + "1101101" + "11")
        self.assertEqual([e for e, _ in events], ["arrived"])
        self.assertTrue(p.here)

    def test_a_run_of_valid_absences_does(self):
        p = log()
        events, _ = run(p, "11" + "0" * pm.LEAVE_AFTER)
        self.assertEqual([e for e, _ in events], ["arrived", "left"])

    def test_looking_at_the_ceiling_is_not_evidence_he_left(self):
        """The rule that matters most. The arm surveying, sweeping or staring
        at a blank wall sees nobody, and that says nothing about him."""
        p = log()
        _, t = run(p, "11")
        events, t = run(p, "0" * 40, start=t, view_ok=False)
        self.assertEqual(events, [])
        self.assertTrue(p.here, "a sweep of the ceiling ended his session")

    def test_seeing_him_from_any_view_counts(self):
        p = log()
        events, _ = run(p, "11", view_ok=False)
        self.assertEqual([e for e, _ in events], ["arrived"])


class WhatItLearns(unittest.TestCase):
    def test_sessions_have_a_start_and_end(self):
        p = log()
        _, t = run(p, "11" + "1" * 30)                 # ~10 minutes
        run(p, "0" * pm.LEAVE_AFTER, start=t)
        s = p.sessions()
        self.assertEqual(len(s), 1)
        self.assertIsNotNone(s[0][1])
        self.assertGreater((s[0][1] - s[0][0]) / 60, 9)

    def test_a_blip_is_not_reported_as_a_session(self):
        p = log()
        _, t = run(p, "11")
        run(p, "0" * pm.LEAVE_AFTER, start=t)
        self.assertEqual(p.sessions(min_minutes=pm.MIN_SESSION_MIN), [])

    def test_an_open_session_has_no_end(self):
        p = log()
        run(p, "1111")
        self.assertIsNone(p.sessions(min_minutes=0)[-1][1])

    def test_routine_counts_only_valid_looks(self):
        p = log()
        run(p, "1" * 12)
        run(p, "0" * 30, start=T0 + 3600, view_ok=False)    # 8pm, not looking
        r = p.routine(min_samples=10)
        self.assertEqual(r[19]["present"], 1.0)
        self.assertTrue(r[19]["enough"])
        self.assertNotIn(20, r, "unwatched hours must not read as 'away'")

    def test_thin_hours_are_flagged(self):
        p = log()
        run(p, "111")
        self.assertFalse(p.routine(min_samples=10)[19]["enough"])

    def test_usual_arrival_needs_several_days(self):
        p = log()
        for day in range(3):
            start = T0 + day * 86400 + day * 600     # 19:00, 19:10, 19:20
            _, t = run(p, "11" + "1" * 15, start=start)
            run(p, "0" * pm.LEAVE_AFTER, start=t)
        s = p.summary(now=T0 + 2 * 86400 + 7200)
        self.assertEqual(s["arrivals_seen"], 3)
        self.assertAlmostEqual(s["usual_arrival_hour"], 19.17, delta=0.2)

    def test_coming_back_from_a_break_is_not_sitting_down(self):
        """Every return used to count, so breaks pushed 'usual' later."""
        p = log()
        for day in range(3):
            start = T0 + day * 86400
            for back in (0, 3600, 7200):           # 19:00, then 20:00, 21:00
                _, t = run(p, "11" + "1" * 15, start=start + back)
                run(p, "0" * pm.LEAVE_AFTER, start=t)
        s = p.summary(now=T0 + 2 * 86400 + 4 * 3600)
        self.assertEqual(s["arrivals_seen"], 3)              # days, not 9
        self.assertAlmostEqual(s["usual_arrival_hour"], 19.0, delta=0.05)
        self.assertIn("usual_leave_hour", s)
        self.assertIn("usual_break_min", s)

    def test_one_day_does_not_claim_a_habit(self):
        p = log()
        _, t = run(p, "11" + "1" * 15)
        run(p, "0" * pm.LEAVE_AFTER, start=t)
        self.assertNotIn("usual_arrival_hour", p.summary(now=T0 + 3600))


class Summary(unittest.TestCase):
    def test_coverage_says_how_much_was_watched(self):
        """Without it, a low 'today_minutes' is ambiguous: away, or unwatched."""
        p = log()
        run(p, "0000", view_ok=True)
        run(p, "0000", start=T0 + 100, view_ok=False)
        self.assertEqual(p.summary(now=T0 + 200)["coverage_today"], 0.5)

    def test_current_session_length(self):
        p = log()
        run(p, "11", start=T0)
        s = p.summary(now=T0 + 600)
        self.assertTrue(s["here"])
        self.assertAlmostEqual(s["for_minutes"], 9.7, delta=0.5)


class Persistence(unittest.TestCase):
    def test_a_restart_mid_session_keeps_him_here(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "p.db")
        p = pm.Presence(path, clock=lambda: T0)
        run(p, "111")
        p.close()
        q = pm.Presence(path, clock=lambda: T0 + 300)
        self.addCleanup(q.close)
        self.assertTrue(q.here, "a dashboard restart invented a departure")

    def test_dev_is_the_default_store(self):
        self.assertEqual(pm.PROFILES["dev"], "presence-dev.db")
        self.assertNotEqual(pm.PROFILES["dev"], pm.PROFILES["live"])



class APictureOfAPerson(unittest.TestCase):
    """08:28-18:19 a 'person' at 0.89 in every sample while he was at
    school. A box that never moves is the evidence."""

    def test_a_box_that_never_moves_is_still(self):
        p = pm.Presence()
        self.addCleanup(p.close)
        for i in range(15):
            p.sample(True, conf=0.89, at=1000 + 20 * i,
                     box=(0.5001, 0.40, 0.21, 0.55))
        self.assertTrue(p.still_person())

    def test_a_living_person_shifts(self):
        import random
        rng = random.Random(3)
        p = pm.Presence()
        self.addCleanup(p.close)
        for i in range(15):
            p.sample(True, conf=0.8, at=1000 + 20 * i,
                     box=(0.5 + rng.uniform(-0.02, 0.02),
                          0.4 + rng.uniform(-0.02, 0.02), 0.2, 0.55))
        self.assertFalse(p.still_person())

    def test_a_still_box_is_learned_as_a_decoy_and_ignored(self):
        """2026-10-09 06:12: it 'arrived' at sunrise, got the rundown and
        a break offer. Five still minutes, then it is not him."""
        p = pm.Presence()
        self.addCleanup(p.close)
        poster = (0.6201, 0.35, 0.18, 0.40)
        for i in range(15):
            p.sample(True, conf=0.89, at=1000 + 20 * i, box=poster)
        self.assertEqual(p.learn_decoy(), poster)
        self.assertIsNone(p.learn_decoy(), "learned once")
        real, fake = p.screen([(0.6202, 0.35, 0.1801, 0.40)])
        self.assertEqual((real, len(fake)), ([], 1))
        # He sits down in front of it: a different, bigger box.
        him = (0.55, 0.55, 0.45, 0.85)
        self.assertEqual(p.screen([him])[0], [him])

    def test_the_decoy_slid_sideways_is_still_the_decoy(self):
        """The seat view lands a few degrees off after a move (backlash):
        the poster slides across the picture but keeps its size."""
        p = pm.Presence()
        self.addCleanup(p.close)
        poster = (0.4734, 0.5625, 0.1273, 0.40)
        for i in range(15):
            p.sample(True, conf=0.89, at=1000 + 20 * i, box=poster)
        p.learn_decoy()
        real, fake = p.screen([(0.55, 0.535, 0.131, 0.402)])
        self.assertEqual((real, len(fake)), ([], 1))
        self.assertIsNone(p.learn_decoy())
        # Measured 09:00 and 09:02: 0.07 lower after the pitch slack.
        for box in ((0.4555, 0.4889, 0.1266, 0.4028),
                    (0.4477, 0.5028, 0.1242, 0.3944)):
            self.assertEqual(p.screen([box])[0], [], box)
        self.assertEqual(p.decoys()[0]["moved"], 0)

    def test_a_decoy_that_moves_was_a_person(self):
        p = pm.Presence()
        self.addCleanup(p.close)
        for i in range(15):
            p.sample(True, conf=0.8, at=1000 + 20 * i,
                     box=(0.5, 0.4, 0.2, 0.55))
        p.learn_decoy()
        for k in range(3):
            # Alive: shifts and changes height, not only slides sideways.
            moved = (0.5 + (0.025 if k % 2 else -0.025), 0.43, 0.2, 0.58)
            self.assertEqual(p.screen([moved])[0], [moved])
        self.assertEqual(p.decoys(), [], "three moves and it is forgotten")

    def test_only_seat_view_samples_count_as_still(self):
        p = pm.Presence()
        self.addCleanup(p.close)
        for i in range(15):
            p.sample(True, conf=0.89, at=1000 + 20 * i, view_ok=False,
                     box=(0.5, 0.4, 0.2, 0.55))
        self.assertIsNone(p.still_person())

    def test_too_few_samples_is_unknown(self):
        p = pm.Presence()
        self.addCleanup(p.close)
        p.sample(True, conf=0.9, box=(0.5, 0.4, 0.2, 0.5))
        self.assertIsNone(p.still_person())

    def test_an_old_store_gains_the_columns(self):
        import sqlite3, tempfile, os
        d = tempfile.mkdtemp()
        path = os.path.join(d, "p.db")
        db = sqlite3.connect(path)
        db.executescript(pm.SCHEMA)
        db.execute("INSERT INTO samples VALUES (1, 1, 0.9, 1)")
        db.commit()
        db.close()
        p = pm.Presence(path)
        self.addCleanup(p.close)
        p.sample(True, conf=0.9, box=(0.5, 0.4, 0.2, 0.5), light=120)
        self.assertEqual(len(p.recent(10)), 2)

if __name__ == "__main__":
    unittest.main()


class ForChecking(unittest.TestCase):
    """An hourly fraction hid a false arrival that the raw looks showed. The
    raw looks have to be reachable."""

    def test_recent_returns_raw_looks_newest_first(self):
        p = log()
        run(p, "1001")
        r = p.recent(3)
        self.assertEqual([x["present"] for x in r], [True, False, False])
        self.assertGreater(r[0]["ts"], r[-1]["ts"])

    def test_a_break_offer_is_logged_and_counted_without_breaking_sessions(self):
        p = log()
        _, t = run(p, "11" + "1" * 10)
        p.log_event("break_offered", at=t)
        run(p, "0" * pm.LEAVE_AFTER, start=t + 20)
        self.assertEqual(p.summary(now=t + 300)["breaks_offered_today"], 1)
        self.assertEqual(len(p.sessions(min_minutes=0)), 1,
                         "a break event must not split or end a session")


class BreakOutcomes(unittest.TestCase):
    def test_leaving_soon_after_is_taking_it(self):
        p = log()
        _, t = run(p, "11" + "1" * 10)
        p.log_event("break_offered", at=t)
        run(p, "0" * pm.LEAVE_AFTER, start=t + 20)
        self.assertEqual(p.break_outcomes(now=t + 3600), [(t, "taken")])

    def test_staying_is_ignoring_it(self):
        p = log()
        _, t = run(p, "11" + "1" * 10)
        p.log_event("break_offered", at=t)
        run(p, "1" * 40, start=t + 20)                 # 13 more minutes
        self.assertEqual(p.break_outcomes(now=t + 3600), [(t, "ignored")])

    def test_a_run_that_ended_is_not_a_no(self):
        p = log()
        _, t = run(p, "11" + "1" * 10)
        p.log_event("break_offered", at=t)             # then nothing watched
        self.assertEqual(p.break_outcomes(now=t + 3600), [])

    def test_too_recent_to_judge(self):
        p = log()
        _, t = run(p, "11" + "1" * 10)
        p.log_event("break_offered", at=t)
        self.assertEqual(p.break_outcomes(now=t + 60), [])


class BackToTheSeat(unittest.TestCase):
    """An arm left anywhere else means he can never be seen to leave."""

    def test_idle_and_elsewhere_goes_back(self):
        self.assertTrue(pm.seat_return_due(100.0, 0.0))

    def test_never_while_he_is_driving_it(self):
        self.assertFalse(pm.seat_return_due(100.0, hold_until=700.0))

    def test_never_while_anything_else_has_the_arm(self):
        for flag in ("busy", "surveying", "paused", "tracking"):
            with self.subTest(flag):
                self.assertFalse(pm.seat_return_due(100.0, 0.0,
                                                    **{flag: True}))

    def test_already_there_does_nothing(self):
        self.assertFalse(pm.seat_return_due(100.0, 0.0, at_seat=True))


class Restarting(unittest.TestCase):
    """2026-10-09 07:21: a restart after a break offer 'arrived' again and
    the morning rundown was said twice."""

    def test_a_break_offer_is_not_the_last_word_on_presence(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "p.db")
        p = pm.Presence(path)
        for i in range(3):
            p.sample(True, at=1000 + 20 * i)
        self.assertTrue(p.here)
        p.log_event("break_offered", at=2000)
        p.close()
        p = pm.Presence(path)
        self.addCleanup(p.close)
        self.assertTrue(p.here)
        self.assertIsNone(p.sample(True, at=2100))
        self.assertIsNone(p.sample(True, at=2120), "no second arrival")
        self.assertEqual(p.arrivals(0), 1)
