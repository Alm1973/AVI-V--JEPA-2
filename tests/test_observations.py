"""What AVI noticed by watching, said back to him."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.perception import observations as ob                 # noqa: E402
from avi_bot.memory import db as memdb                            # noqa: E402
from avi_bot.skills import memory_tools                           # noqa: E402

LAYOUT = [{"label": "pen", "where": "In front of and left of the laptop, "
                                   "nearer to me."}]


class Notes(unittest.TestCase):
    def test_here_now(self):
        got = ob.notes({"here": True, "for_minutes": 23.4})
        self.assertEqual(got, ["You've been at the desk 23 minutes."])

    def test_a_habit_only_when_presence_has_one(self):
        self.assertFalse(any("usually" in s for s in ob.notes(
            {"today_minutes": 12.6})))
        got = ob.notes({"usual_arrival_hour": 19.17})
        self.assertIn("You usually sit down around 7:10 PM.", got)

    def test_the_shape_of_his_day(self):
        got = ob.notes({"usual_arrival_hour": 19.0,
                        "usual_leave_hour": 24.5,
                        "usual_session_min": 95,
                        "usual_break_min": 25})
        self.assertIn("You usually finish around 12:30 AM.", got)
        self.assertIn("You usually sit for about 1 hour 30 minutes at a "
                      "time.", got)
        self.assertIn("Your breaks usually last about 25 minutes.", got)

    def test_whether_he_takes_the_breaks_once_there_is_a_pattern(self):
        self.assertFalse(any("breaks I suggested" in x for x in ob.notes(
            {"breaks_judged": 2, "breaks_taken": 1})))
        self.assertIn("You took 1 of the last 4 breaks I suggested.",
                      ob.notes({"breaks_judged": 4, "breaks_taken": 1}))

    def test_where_things_are(self):
        self.assertIn("Your pen was in front of and left of the laptop, "
                      "nearer to me.", ob.notes(layout=LAYOUT))

    def test_a_question_about_the_pen_gets_the_pen(self):
        s = ob.notes({"today_minutes": 12}, LAYOUT)
        self.assertEqual(len(ob.about("my pen", s)), 1)


class WhatDoYouKnow(unittest.TestCase):
    def tool(self, observed):
        store = memdb.Store()
        self.addCleanup(store.close)
        return store, memory_tools.make(store, observed=observed)[
            "what_do_you_know"]

    def test_told_and_noticed_together(self):
        store, know = self.tool(lambda: ob.notes({"today_minutes": 12},
                                                 LAYOUT))
        store.remember("Chemistry test on Friday")
        text, data = know()
        self.assertIn("Chemistry test on Friday.", text)
        self.assertIn("You were at the desk 12 minutes today.", text)
        self.assertLessEqual(len(text.split()), 20, "speech cap")

    def test_nothing_told_still_has_something_noticed(self):
        _, know = self.tool(lambda: ob.notes(layout=LAYOUT))
        self.assertIn("Your pen", know()[0])

    def test_watching_failing_does_not_lose_what_he_said(self):
        def broken():
            raise RuntimeError("camera gone")
        store, know = self.tool(broken)
        store.remember("Chemistry test on Friday")
        self.assertIn("Chemistry", know()[0])


if __name__ == "__main__":
    unittest.main()
