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


def log():
    return pm.Presence(":memory:", clock=lambda: T0)


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
