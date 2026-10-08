"""The memory tables and the prompt-assembly rules.

The fact-store limits and the never-send list are both load-bearing, so both
are tested as behaviour rather than trusted as comments.
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.brain import context                             # noqa: E402
from avi_bot.memory import db as memdb                        # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s=1.0):
        self.t += s
        return self.t


class Facts(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = memdb.Store(clock=self.clock)
        self.addCleanup(self.store.close)

    def test_round_trip(self):
        self.store.remember("he drinks tea, not coffee")
        self.assertIn("he drinks tea, not coffee", self.store.recall("tea"))

    def test_duplicates_do_not_accumulate(self):
        a, _ = self.store.remember("the lamp is on the left")
        b, _ = self.store.remember("the lamp is on the left")
        self.assertEqual(a, b)
        self.assertEqual(self.store.count_facts(), 1)

    def test_too_long_is_refused_not_truncated(self):
        """A silently cut fact is a changed fact, which is worse than none."""
        with self.assertRaises(ValueError):
            self.store.remember("x" * (memdb.MAX_FACT_CHARS + 1))

    def test_the_cap_holds(self):
        for i in range(memdb.MAX_FACTS + 20):
            self.clock.tick()
            self.store.remember(f"fact number {i}")
        self.assertEqual(self.store.count_facts(), memdb.MAX_FACTS)

    def test_the_oldest_UNUSED_fact_is_dropped_first(self):
        """Something he brings up constantly must be the last thing forgotten,
        so eviction is by use, not purely by age."""
        self.store.remember("the oldest fact, but much loved")
        for i in range(memdb.MAX_FACTS - 1):
            self.clock.tick()
            self.store.remember(f"filler {i}")
        for _ in range(5):                       # use the oldest repeatedly
            self.store.recall("much loved")
        self.clock.tick()
        _, dropped = self.store.remember("one more, forcing an eviction")
        self.assertIsNotNone(dropped)
        self.assertNotIn("much loved", dropped)
        self.assertIn("the oldest fact, but much loved",
                      self.store.recall("much loved"))

    def test_forget(self):
        self.store.remember("a passing thought")
        self.assertEqual(self.store.forget("a passing thought"), 1)
        self.assertEqual(self.store.count_facts(), 0)

    def test_recall_caps_at_the_prompt_budget(self):
        for i in range(40):
            self.clock.tick()
            self.store.remember(f"thing about school number {i}")
        self.assertLessEqual(len(self.store.recall("school")),
                             context.MAX_FACTS)

    def test_empty_fact_is_refused(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                self.store.remember(bad)


class Citations(unittest.TestCase):
    """Retrieval is a guess about relevance; a citation is evidence of it.

    Reflective Memory Management (arXiv 2503.08026) reports >10% on exactly
    this distinction, and the cheap version is one column.
    """

    def setUp(self):
        self.clock = Clock()
        self.store = memdb.Store(clock=self.clock)
        self.addCleanup(self.store.close)

    def test_a_cited_fact_outranks_a_merely_retrieved_one(self):
        self.store.remember("he has chemistry on Tuesdays")
        self.store.remember("he has chemistry homework somewhere")
        for _ in range(5):                      # both retrieved repeatedly
            self.store.recall("chemistry")
        self.store.cite("he has chemistry homework somewhere")
        self.assertEqual(self.store.recall("chemistry", limit=1),
                         ["he has chemistry homework somewhere"])

    def test_citing_something_unknown_is_harmless(self):
        self.assertEqual(self.store.cite("a fact that was never stored"), 0)

    def test_dead_weight_is_findable(self):
        """Retrieved often, never cited: actively costing prompt budget."""
        self.store.remember("a fact that never helps")
        self.store.remember("a fact that does help")
        for _ in range(4):
            self.store.recall()
        self.store.cite("a fact that does help")
        dead = self.store.dead_weight(min_uses=3)
        self.assertIn("a fact that never helps", dead)
        self.assertNotIn("a fact that does help", dead)

    def test_eviction_prefers_the_never_cited(self):
        self.store.remember("cited once, keep me")
        self.store.cite("cited once, keep me")
        for i in range(memdb.MAX_FACTS - 1):
            self.clock.tick()
            self.store.remember(f"never cited filler {i}")
        self.clock.tick()
        _, dropped = self.store.remember("forcing an eviction")
        self.assertIsNotNone(dropped)
        self.assertNotEqual(dropped, "cited once, keep me")

    def test_cite_accepts_a_list(self):
        self.store.remember("one")
        self.store.remember("two")
        self.assertEqual(self.store.cite(["one", "two", "three"]), 2)

    def test_blank_citations_are_skipped(self):
        self.store.remember("real")
        self.assertEqual(self.store.cite(["", "   ", None]), 0)


class Reminders(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = memdb.Store(clock=self.clock)
        self.addCleanup(self.store.close)

    def test_only_due_ones_come_back(self):
        self.store.add_reminder(self.clock.t + 10, "stand up")
        self.store.add_reminder(self.clock.t - 10, "drink water")
        due = self.store.due_reminders()
        self.assertEqual([d["text"] for d in due], ["drink water"])

    def test_due_does_not_mark_them_done(self):
        """A crash between reading and speaking should lose the reminder
        visibly, not eat it silently."""
        self.store.add_reminder(self.clock.t - 1, "call home")
        self.assertEqual(len(self.store.due_reminders()), 1)
        self.assertEqual(len(self.store.due_reminders()), 1)
        rid = self.store.due_reminders()[0]["id"]
        self.store.mark_reminder(rid)
        self.assertEqual(self.store.due_reminders(), [])


class Feedback(unittest.TestCase):
    def setUp(self):
        self.store = memdb.Store()
        self.addCleanup(self.store.close)

    def test_never_asked_is_not_always_refused(self):
        """A proactive policy that confuses the two will either never speak or
        never stop."""
        self.assertIsNone(self.store.acceptance_rate())
        self.store.log_feedback("take a break?", "no")
        self.assertEqual(self.store.acceptance_rate(), 0.0)

    def test_rate_counts_yes(self):
        for r in ("yes", "yes", "no", "accepted"):
            self.store.log_feedback("break?", r)
        self.assertAlmostEqual(self.store.acceptance_rate(), 0.75)


class NeverSent(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 22, 40)

    def test_a_key_never_reaches_the_prompt(self):
        block = context.build(now=self.NOW,
                              facts=["his api key is gsk_ABCDEFGHIJKLMNOP"])
        self.assertNotIn("gsk_ABCDEFGHIJKLMNOP", block)

    def test_the_calendar_url_never_reaches_the_prompt(self):
        url = "https://calendar.google.com/calendar/ical/s3cr3t/basic.ics"
        block = context.build(now=self.NOW, facts=[f"calendar at {url}"])
        self.assertNotIn("s3cr3t", block)

    def test_there_is_no_free_form_parameter_to_smuggle_a_frame_through(self):
        """build() takes typed arguments on purpose."""
        with self.assertRaises(TypeError):
            context.build(now=self.NOW, extra="<a base64 frame>")

    def test_redaction_happens_before_truncation(self):
        """A half-cut key must not survive the cut."""
        key = "gsk_" + "A" * 40
        block = context.build(now=self.NOW, facts=[f"key {key}"],
                              max_chars=40)
        self.assertNotIn("gsk_AAAA", block)


class Budgeted(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 22, 40)

    def test_the_block_fits_the_free_tier(self):
        block = context.build(
            now=self.NOW, personality="x" * 4000,
            activity="typing", objects=[f"object {i}" for i in range(40)],
            mood="curious",
            events=[{"when": "10:00", "what": f"event {i}"} for i in range(9)],
            facts=[f"fact {i} " + "y" * 300 for i in range(40)],
            turns=[{"who": "him", "text": "z" * 500} for _ in range(30)])
        self.assertLessEqual(len(block), context.MAX_CHARS)

    def test_only_three_events_and_six_turns(self):
        block = context.build(
            now=self.NOW,
            events=[{"when": f"{h}:00", "what": f"event{h}"}
                    for h in range(9, 18)],
            turns=[{"who": "him", "text": f"turn{i}"} for i in range(20)])
        self.assertIn("event9", block)
        self.assertNotIn("event13", block)
        self.assertIn("turn19", block)
        self.assertNotIn("turn13", block)

    def test_nothing_in_means_nothing_out(self):
        self.assertEqual(context.build(), "")

    def test_the_scene_as_words(self):
        block = context.build(camera=["person (centre, 0.92)",
                                      "laptop (bottom left, 0.81)"],
                              pose="Pose now: base_yaw 94 (= ATTENTIVE).",
                              noticed=["Your pen was left of the laptop."])
        self.assertIn("Camera now: person (centre, 0.92); laptop (bottom "
                      "left, 0.81).", block)
        self.assertIn("Pose now: base_yaw 94", block)
        self.assertIn("You noticed: Your pen was left of the laptop.", block)

    def test_what_is_on_reaches_the_prompt(self):
        self.assertIn("On right now: focus mode (12 of 25 min).",
                      context.build(modes="focus mode (12 of 25 min)"))

    def test_the_desk_distances_reach_the_prompt(self):
        block = context.build(desk="Desk at the last scan (5 h ago; things "
                                   "may have moved since), distance from "
                                   "you: pen 0.8 m.")
        self.assertIn("pen 0.8 m", block)

    def test_an_empty_camera_says_so(self):
        self.assertIn("nothing recognised", context.build(camera=[]))

    def test_about_me_with_no_word_match_still_gets_the_facts(self):
        store = memdb.Store()
        self.addCleanup(store.close)
        store.remember("chemistry test on Friday")
        block = context.from_store(store, utterance="what do you know about "
                                                    "me", now=self.NOW)
        self.assertIn("chemistry test on Friday", block)

    def test_from_store_recalls_against_the_utterance(self):
        store = memdb.Store()
        self.addCleanup(store.close)
        store.remember("his chemistry teacher is called Ruiz")
        store.remember("he hates mushrooms")
        block = context.from_store(store, utterance="what did my chemistry "
                                                    "teacher say", now=self.NOW)
        self.assertIn("Ruiz", block)
        self.assertNotIn("mushrooms", block)


if __name__ == "__main__":
    unittest.main()
