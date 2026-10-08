"""The assembly, with no hardware at all.

The point of these tests is that the whole robot can be constructed and driven
headless: no arm, no camera, no serial port, no network, no keys. If that ever
stops being true, remote development stops working.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot import main as main_mod                           # noqa: E402
from avi_bot.brain import providers, router as router_mod      # noqa: E402

ICS = (Path(__file__).resolve().parents[1] /
       "fixtures" / "ics" / "desk.ics").read_text()


class Clock:
    # 2026-10-06 07:00 America/Phoenix, which is a date the ICS fixture
    # actually covers. The default used to be an arbitrary 2025 timestamp, and
    # that only worked because the calendar was reading real time instead of
    # this clock: test_the_schedule_comes_from_the_fixture asked for "today"
    # and got the real today, which happened to be the fixture's 2026-10-06.
    # It would have failed on any other day of the year.
    #
    # 07:00 is also chosen to sit clear of every reminder window in the
    # fixture -- 08:00, 10:00, 14:00 and 18:30 -- so a tick() in an unrelated
    # test is not interrupted by a calendar warning.
    def __init__(self, t=1_791_295_200.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s
        return self.t


def offline(name, url, model, messages, tool_list):
    raise providers.ProviderError(name, "no network in tests")


def build(**kw):
    kw.setdefault("send", offline)
    kw.setdefault("env", {})
    kw.setdefault("ics_text", ICS)
    return main_mod.Avi(**kw)


class Construction(unittest.TestCase):
    def test_it_builds_with_no_hardware(self):
        avi = build()
        self.addCleanup(avi.close)
        self.assertIsNone(avi.arm)

    def test_every_tool_has_a_handler(self):
        """A tool in the schema with nothing behind it is a promise the robot
        cannot keep, and the model has been told it can."""
        avi = build()
        self.addCleanup(avi.close)
        self.assertEqual(avi.skills.missing(), [])

    def test_a_missing_handler_is_loud(self):
        """Not a warning. The failure mode is the model calling it in a month."""
        from avi_bot.brain import tools
        # Add a tool to the SCHEMA that nothing implements. That is the real
        # failure: someone writes a schema entry, the model is told the robot
        # can do it, and nobody notices for a month. Popping a handler would
        # not test this, because _wire() simply puts it back.
        tools.BY_NAME["polish_the_brass"] = {"type": "function", "function": {
            "name": "polish_the_brass", "description": "x",
            "parameters": {"type": "object", "properties": {},
                           "required": []}}}
        try:
            with self.assertRaises(RuntimeError) as caught:
                build().close()
            self.assertIn("polish_the_brass", str(caught.exception))
        finally:
            tools.BY_NAME.pop("polish_the_brass", None)


class WhatTheCloudIsTold(unittest.TestCase):
    """The context block existed with slots for the time, his activity and
    the desk objects; every turn passed none of them."""

    def sent(self, **kw):
        seen = []

        def send(name, url, model, messages, tool_list):
            seen.append(messages)
            raise providers.ProviderError(name, "captured")
        avi = build(send=send, env={"GROQ_API_KEY": "x"}, **kw)
        self.addCleanup(avi.close)
        avi.handle("ruminate upon belt tension")
        return "\n".join(m["content"] for m in seen[0])

    def test_the_time_of_day(self):
        self.assertIn("Now: ", self.sent())

    def test_where_he_is_and_what_is_on_the_desk(self):
        text = self.sent(situation=lambda: {
            "activity": "at the desk for 40 minutes",
            "objects": ["laptop", "mug"]})
        self.assertIn("He appears to be: at the desk for 40 minutes.", text)
        self.assertIn("Verified on the desk: laptop, mug.", text)

    def test_a_broken_situation_does_not_lose_the_turn(self):
        def boom():
            raise RuntimeError("camera gone")
        self.assertIn("Now: ", self.sent(situation=boom))


class FirstSitDownOfTheDay(unittest.TestCase):
    """calendar.rundown was built and tested, and nothing ever called it."""

    def test_whats_ahead_today_once(self):
        clock = Clock()
        avi = build(clock=clock, ics_text=None)
        self.addCleanup(avi.close)
        avi.store.add_reminder(clock() + 3 * 3600, "call mom")
        avi.store.add_reminder(clock() - 3600, "already past")
        avi.store.add_reminder(clock() + 2 * 86400, "not today")
        text = avi.rundown()
        self.assertTrue(text.startswith("Today: call mom at 10:00 AM"), text)
        self.assertNotIn("past", text)
        self.assertNotIn("not today", text)
        self.assertIsNone(avi.rundown(), "twice in one day")

    def test_nothing_ahead_is_silence_not_nothing_on_today(self):
        avi = build(clock=Clock(), ics_text=None)
        self.addCleanup(avi.close)
        self.assertIsNone(avi.rundown())

    def test_the_calendar_joins_in_when_there_is_one(self):
        avi = build(clock=Clock())
        self.addCleanup(avi.close)
        text = avi.rundown()
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith("Today: "))


class MoodFollowsWhatHappens(unittest.TestCase):
    """Focus, task and curiosity events were only reachable from buttons."""

    def avi(self):
        from avi_bot.personality import mood as mood_mod
        clock = Clock()
        mood = mood_mod.Mood(clock=clock)
        avi = build(clock=clock, mood=mood)
        self.addCleanup(avi.close)
        return avi, mood

    def test_a_focus_session_quiets_the_robot_and_ending_it_releases(self):
        avi, mood = self.avi()
        avi.handle("focus for 25 minutes")
        self.assertTrue(mood.focus, "idle behaviour would fidget through it")
        avi.focus.stop()
        self.assertFalse(mood.focus)

    def test_doing_what_he_asked_lifts_the_mood(self):
        avi, mood = self.avi()
        before = mood.valence
        avi.handle("set a timer for 5 minutes")
        self.assertGreater(mood.valence, before)
        self.assertEqual(mood.log[-1][1], "task_ok")

    def test_a_turn_with_nothing_to_do_changes_nothing(self):
        avi, mood = self.avi()
        avi.handle("ruminate upon belt tension")
        self.assertEqual(mood.log, [])


class MotionArm:
    """Records joint commands; clamps like the real one."""

    class config:
        @staticmethod
        def soft_limits(j):
            return {"base_yaw": (20, 160), "wrist": (20, 176)}.get(j, (25, 155))

    def __init__(self):
        self.pose = {"base_yaw": 90.0, "shoulder": 60.0, "elbow": 100.0,
                     "wrist": 150.0}
        self.sent, self.held, self.recalled = [], [], []

    def snapshot(self):
        return dict(self.pose)

    def set(self, j, deg):
        lo, hi = self.config.soft_limits(j)
        self.pose[j] = max(lo, min(hi, float(deg)))
        self.sent.append((j, round(self.pose[j], 1)))
        return self.pose[j]

    def hold(self, s):
        self.held.append(s)

    def recall(self, name):
        self.recalled.append(name)


class FreeMotion(unittest.TestCase):
    """What the model improvises with."""

    def setUp(self):
        self.arm = MotionArm()
        self.avi = build(clock=Clock(), arm=self.arm)
        self.addCleanup(self.avi.close)
        self.avi._sleep = lambda s: None
        self.avi.background = False

    def run_call(self, tool, **args):
        return self.avi.skills.run(tool, args)

    def test_nudge_left_turns_the_base_down(self):
        self.assertTrue(self.run_call("nudge", direction="left",
                                      degrees=10).ok)
        self.assertAlmostEqual(self.arm.pose["base_yaw"], 80.0)
        self.assertTrue(self.arm.held, "the seat loop would undo it")

    def test_nudge_up_raises_the_wrist(self):
        self.run_call("nudge", direction="up", degrees=12)
        self.assertAlmostEqual(self.arm.pose["wrist"], 162.0)

    def test_a_wave_plays_and_comes_back(self):
        steps = [{"wrist": 170, "hold_s": 0.3}, {"wrist": 140, "hold_s": 0.3},
                 {"wrist": 170, "hold_s": 0.3}, {"wrist": 140, "hold_s": 0.3}]
        r = self.run_call("motion_sequence", steps=steps)
        self.assertTrue(r.ok, r.error)
        wrist = [d for j, d in self.arm.sent if j == "wrist"]
        self.assertEqual(wrist, [170.0, 140.0, 170.0, 140.0, 150.0])

    def test_move_joints_is_clamped_by_the_arm(self):
        self.run_call("move_joints", wrist=5)
        self.assertEqual(self.arm.pose["wrist"], 20.0)

    def test_a_move_the_picture_disagrees_with_is_said(self):
        verdicts = []

        def judged_move(direction, do):
            do()
            v = {"commanded": direction, "votes": {"orb": "none"},
                 "judged": True, "agrees": False, "motion": {}}
            verdicts.append(v)
            return v
        self.arm.judged_move = judged_move
        r = self.run_call("nudge", direction="left", degrees=10)
        self.assertEqual(r.text, "I tried to look left, but the camera "
                                 "didn't move.")
        self.assertEqual(len(verdicts), 2, "one retry when nothing moved")
        self.assertTrue(r.data["verdict"]["retried"])

    def test_a_move_the_picture_confirms_is_silent(self):
        def judged_move(direction, do):
            do()
            return {"commanded": direction, "votes": {"orb": direction},
                    "judged": True, "agrees": True, "motion": {}}
        self.arm.judged_move = judged_move
        r = self.run_call("nudge", direction="right", degrees=10)
        self.assertIsNone(r.text)
        self.assertTrue(r.data["verdict"]["agrees"])

    def test_what_it_sees_in_words(self):
        self.avi.situation = lambda: {"camera": ["person (centre, 0.92)",
                                                 "mug (bottom left, 0.71)"]}
        r = self.run_call("describe_view")
        self.assertEqual(r.text, "I see you (centre), a mug (bottom left).")

    def test_look_around_sweeps_and_names_what_it_saw_where(self):
        views = {80.0: ["mug (centre, 0.8)"], 110.0: ["laptop (centre, 0.9)",
                 "person (left, 0.9)"], 140.0: ["bottle (right, 0.6)"]}
        self.arm.pose["base_yaw"] = 110.0
        self.avi.situation = lambda: {"camera": views.get(
            round(self.arm.pose["base_yaw"]), [])}
        r = self.run_call("look_around", count=3)
        self.assertEqual(r.text, "I see a mug (left), a laptop (ahead), "
                                 "a bottle (right).")
        self.assertAlmostEqual(self.arm.pose["base_yaw"], 110.0,
                               msg="came back to where it started")

    def test_fewer_than_asked_in_the_dark_says_why(self):
        self.arm.pose["base_yaw"] = 110.0
        self.avi.situation = lambda: {
            "camera": (["laptop (right, 0.5)"]
                       if round(self.arm.pose["base_yaw"]) == 140 else []),
            "light": "dim (39/255)"}
        r = self.run_call("look_around", count=3)
        self.assertEqual(r.text, "I can only make out a laptop (right). "
                                 "It's too dark to make out more.")

    def test_the_dim_room_reaches_the_prompt(self):
        from avi_bot.brain import context
        self.assertIn("The room is dim (39/255)",
                      context.build(camera=[], light="dim (39/255)"))

    def test_an_answer_tool_is_never_muted(self):
        """'look around and describe 3 things u see' got silence: the reply
        was treated as an action, and actions do not speak."""
        from avi_bot.brain import router as router_mod, speech
        r = router_mod.Router(chain=None)
        self.assertEqual(r._reason("look around and describe 3 things u see",
                                   [("describe_view", {})]), speech.ANSWER)
        self.assertTrue(speech.is_question("describe what you see"))

    def test_a_look_that_lands_on_nothing_is_said(self):
        """'look at the laptop' ended on a mug, silently (2026-10-08)."""
        self.arm.look_at = lambda t: {"moved": True, "in_view": False,
                                      "say": "Behind the book."}
        self.assertEqual(self.run_call("look_at", target="laptop").text,
                         "I can't see the laptop from here.")

    def test_a_look_that_lands_is_silent(self):
        self.arm.look_at = lambda t: {"moved": True, "in_view": True}
        self.assertIsNone(self.run_call("look_at", target="laptop").text)

    def test_no_aim_says_what_it_knows(self):
        self.arm.look_at = lambda t: {"moved": False,
                                      "say": "I can't recognise a charger "
                                             "yet."}
        self.assertEqual(self.run_call("look_at", target="charger").text,
                         "I can't recognise a charger yet.")

    def test_nobody_in_frame_for_look_at_me(self):
        self.arm.look_at = lambda t: {"looked": False}
        self.assertEqual(self.run_call("look_at", target="me").text,
                         "I can't see you from here.")

    def test_a_taught_pose_by_name(self):
        self.run_call("go_to_pose", name="TALL")
        self.assertEqual(self.arm.recalled, ["TALL"])


class Turns(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.avi = build(clock=self.clock)
        self.addCleanup(self.avi.close)

    def test_a_local_command_executes_without_the_cloud(self):
        decision, results = self.avi.handle("set a timer for 5 minutes")
        self.assertEqual(decision.route, router_mod.LOCAL)
        self.assertTrue(all(r.ok for r in results), [r.error for r in results])
        self.assertEqual(len(self.avi.timers.pending()), 1)

    def test_a_skill_answer_overrides_the_model(self):
        """The skill knows; the model was guessing."""
        decision, results = self.avi.handle("what timers are there")
        self.assertIn("timer", decision.reply.say.lower())

    def test_an_action_gets_no_words(self):
        decision, results = self.avi.handle("turn the lamp off")
        self.assertIsNone(decision.reply.say)
        self.assertIsNotNone(decision.reply.beep)

    def test_the_schedule_comes_from_the_fixture(self):
        decision, results = self.avi.handle("what's on my schedule today")
        self.assertTrue(results[0].ok, results[0].error)
        self.assertIsNotNone(decision.reply.say)

    def test_an_unroutable_utterance_gives_a_confused_chirp(self):
        decision, _ = self.avi.handle("ruminate upon belt tension")
        self.assertEqual(decision.route, router_mod.CONFUSED)
        self.assertEqual(decision.reply.beep, "confused")

    def test_handle_never_raises(self):
        for text in ("", None, "x" * 4000, "???", "turn the lamp off"):
            decision, results = self.avi.handle(text)
            self.assertIsNotNone(decision.reply)

    def test_turns_accumulate_for_context(self):
        self.avi.handle("what time is it")
        self.assertGreaterEqual(len(self.avi.turns), 1)
        self.assertEqual(self.avi.turns[0]["who"], "him")

    def test_interactions_are_logged(self):
        self.avi.handle("turn the lamp off")
        rows = self.avi.store.db.execute(
            "SELECT * FROM interactions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"], router_mod.LOCAL)


class GuestModeStopsLogging(unittest.TestCase):
    def test_nothing_is_written_with_a_guest_present(self):
        """Fails closed: the camera points at someone who did not agree."""
        avi = build()
        self.addCleanup(avi.close)
        avi.handle("guest mode")
        self.assertFalse(avi.logging_enabled)
        avi.handle("what time is it")
        rows = avi.store.db.execute(
            "SELECT COUNT(*) c FROM interactions").fetchone()
        # ZERO, not one. handle() logs after the skills run, so guest mode is
        # already in force by the time its own command would be written -- the
        # utterance that turned it on is not recorded either. That is the
        # conservative reading and the right one: the moment someone asks for
        # privacy is not the moment to write one last row.
        self.assertEqual(rows["c"], 0,
                         "logged something during guest mode")


class Ticking(unittest.TestCase):
    def test_a_due_timer_surfaces_through_tick(self):
        clock = Clock()
        avi = build(clock=clock)
        self.addCleanup(avi.close)
        avi.handle("set a timer for 5 minutes")
        self.assertIsNone(avi.tick())
        clock.tick(301)
        announcement = avi.tick()
        self.assertIsNotNone(announcement)
        self.assertEqual(announcement.text, "Your 5 minute timer is done.")

    def test_tick_is_silent_in_quiet_mode(self):
        clock = Clock()
        avi = build(clock=clock)
        self.addCleanup(avi.close)
        avi.handle("set a timer for 5 minutes")
        avi.handle("be quiet for 30 minutes")
        clock.tick(301)
        self.assertIsNone(avi.tick())


class ArmFailuresDoNotReachTheBrain(unittest.TestCase):
    class AngryArm:
        def __init__(self):
            self.config = type("C", (), {"camera": {
                "yaw_deg_per_frame": 83.9}})()

        def __getattr__(self, name):
            def boom(*a, **k):
                raise OSError("the serial port went away")
            return boom

    def test_a_failing_arm_does_not_cost_him_a_timer(self):
        avi = build(arm=self.AngryArm())
        self.addCleanup(avi.close)
        decision, results = avi.handle("set a timer for 5 minutes")
        self.assertTrue(all(r.ok for r in results))
        decision, results = avi.handle("stop")
        self.assertTrue(all(r.ok for r in results),
                        [r.error for r in results])


if __name__ == "__main__":
    unittest.main()


class OneTimeline(unittest.TestCase):
    """Avi must not run on two clocks at once.

    The calendar was built without `clock=`, so it read real time while timers,
    focus and modes read the injected one. The visible symptom was a test that
    failed only in the ten minutes before the fixture's 10:00 Chemistry -- a
    one-minute window, once a day, on a machine nobody was watching. The
    invisible one is worse: no calendar behaviour reachable through Avi could
    be pinned to a time, so none of it was really under test.
    """

    def test_the_calendar_uses_avis_clock(self):
        clock = Clock()
        avi = build(clock=clock)
        self.addCleanup(avi.close)
        # The calendar returns an aware datetime where the rest of AVI
        # returns epoch seconds, so compare the instant, not the type.
        self.assertAlmostEqual(avi.calendar.clock().timestamp(), clock(),
                               places=3,
                               msg="the calendar is reading a different clock")

    def test_every_clocked_part_agrees(self):
        clock = Clock()
        avi = build(clock=clock)
        self.addCleanup(avi.close)
        for name in ("timers", "focus", "modes", "calendar"):
            part = getattr(avi, name, None)
            got = getattr(part, "clock", None)
            if callable(got):
                with self.subTest(name):
                    now = got()
                    now = now.timestamp() if hasattr(now, "timestamp") else now
                    self.assertAlmostEqual(
                        now, clock(), places=3,
                        msg=f"{name} is on a different timeline")

    def test_the_default_clock_lands_on_a_day_the_fixture_covers(self):
        """If the pinned clock and the fixture's dates ever drift apart,
        "today" silently becomes an empty day and every schedule test starts
        passing or failing by calendar coincidence rather than by behaviour."""
        avi = build(clock=Clock())
        self.addCleanup(avi.close)
        self.assertTrue(avi.calendar.events("today"),
                        "the ICS fixture has no events on the pinned date")

    def test_a_pinned_clock_gives_a_stable_tick(self):
        """The regression itself: tick() must not depend on the wall clock."""
        clock = Clock()
        avi = build(clock=clock)
        self.addCleanup(avi.close)
        self.assertIsNone(avi.tick())
        self.assertIsNone(avi.tick())
