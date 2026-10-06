"""M5 skills, against the acceptance tests BUILD_SPEC 9 names for each.

None of these need the arm, the camera, the network or a GPU. The parts that
do touch the world -- launching an app, calling Open-Meteo -- are injected, so
what gets tested is the decision rather than the side effect.
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.memory import db as memdb                        # noqa: E402
from avi_bot.skills import apps as apps_mod                   # noqa: E402
from avi_bot.skills import find as find_mod                   # noqa: E402
from avi_bot.skills import timers as timers_mod               # noqa: E402
from avi_bot.skills import weather as weather_mod             # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class Clock:
    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s
        return self.t


# --- timers: "a 5 s timer fires, and still fires after a restart" ---------
class Timers(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.path = ":memory:"
        self.store = memdb.Store(clock=self.clock)
        self.addCleanup(self.store.close)
        self.t = timers_mod.Timers(store=self.store, clock=self.clock)

    def test_a_five_second_timer_fires(self):
        tid = self.t.set(5)
        self.assertEqual(self.t.due(), [])
        self.clock.tick(5)
        due = self.t.due()
        self.assertEqual([d["id"] for d in due], [tid])

    def test_it_still_fires_after_a_restart(self):
        """The half of the test that drives the design. The dashboard
        restarted a dozen times in one evening; an in-memory timer would have
        vanished each time, silently."""
        path = str(Path(self.id().split(".")[-1] + ".sqlite3"))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "timers.sqlite3")
            store = memdb.Store(path, clock=self.clock)
            timers_mod.Timers(store=store, clock=self.clock).set(5)
            store.close()                       # the "restart"
            self.clock.tick(6)
            store2 = memdb.Store(path, clock=self.clock)
            self.addCleanup(store2.close)
            revived = timers_mod.Timers(store=store2, clock=self.clock)
            self.assertEqual(len(revived.due()), 1,
                             "the timer did not survive a restart")

    def test_due_does_not_consume(self):
        self.t.set(1)
        self.clock.tick(2)
        self.assertEqual(len(self.t.due()), 1)
        self.assertEqual(len(self.t.due()), 1, "consumed on read")
        self.t.fired(self.t.due()[0]["id"])
        self.assertEqual(self.t.due(), [])

    def test_cancel_one_and_cancel_all(self):
        a = self.t.set(60)
        self.t.set(120)
        self.assertEqual(self.t.cancel(a), 1)
        self.assertEqual(len(self.t.pending()), 1)
        self.assertEqual(self.t.cancel(), 1)
        self.assertEqual(self.t.pending(), [])

    def test_a_zero_second_timer_is_refused(self):
        for bad in (0, -5):
            with self.assertRaises(ValueError):
                self.t.set(bad)

    def test_remaining_counts_down(self):
        tid = self.t.set(60)
        self.clock.tick(20)
        self.assertAlmostEqual(self.t.remaining(tid), 40.0, places=1)
        self.clock.tick(100)
        self.assertEqual(self.t.remaining(tid), 0.0, "went negative")

    def test_describe_fits_the_speech_cap(self):
        from avi_bot.brain import speech
        self.assertEqual(self.t.describe(), "No timers.")
        self.t.set(1200)
        for text in (self.t.describe(),):
            self.assertLessEqual(len(text.split()), speech.MAX_WORDS)
        self.t.set(60)
        self.assertIn("2 timers", self.t.describe())


# --- apps: "an unlisted app is refused" ----------------------------------
class Apps(unittest.TestCase):
    def setUp(self):
        self.opened = []
        self.apps = apps_mod.Apps(
            table={"Notepad": "notepad.exe", "notes": "https://keep.google.com"},
            launch=self.opened.append)

    def test_an_unlisted_app_is_refused(self):
        ok, why = self.apps.open("powershell")
        self.assertFalse(ok)
        self.assertEqual(self.opened, [], "launched something unlisted")
        self.assertIn("not on the list", why)

    def test_a_listed_app_opens(self):
        ok, _ = self.apps.open("notepad")
        self.assertTrue(ok)
        self.assertEqual(self.opened, ["notepad.exe"])

    def test_matching_ignores_case_and_spacing(self):
        ok, _ = self.apps.open("  NOTEPAD ")
        self.assertTrue(ok)

    def test_a_path_cannot_be_smuggled_in(self):
        """The name is a key into the table, never a path."""
        for attempt in ("C:\\Windows\\System32\\cmd.exe", "../../bin/sh",
                        "notepad.exe; rm -rf /", "https://evil.example"):
            ok, _ = self.apps.open(attempt)
            self.assertFalse(ok, attempt)
        self.assertEqual(self.opened, [])

    def test_the_real_config_parses_and_lists_apps(self):
        real = apps_mod.Apps(path=str(
            Path(__file__).resolve().parents[1] / "config" / "apps.toml"))
        self.assertIn("calculator", real.known())
        self.assertIsNone(real.resolve("cmd"))

    def test_a_missing_config_is_an_empty_whitelist_not_a_crash(self):
        empty = apps_mod.Apps(path="no/such/apps.toml")
        self.assertEqual(empty.known(), [])
        self.assertFalse(empty.open("notepad")[0])

    def test_a_launch_failure_is_reported_not_raised(self):
        def boom(target):
            raise OSError("not found")
        a = apps_mod.Apps(table={"x": "x.exe"}, launch=boom)
        ok, why = a.open("x")
        self.assertFalse(ok)
        self.assertIn("could not open", why)


# --- weather: "fixture JSON -> the right sentence" -----------------------
class Weather(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(
            (FIXTURES / "weather" / "phoenix.json").read_text())

    def test_today_from_the_fixture(self):
        text, data = weather_mod.describe(self.payload, day="today")
        self.assertEqual(text, "Today: clear, 98 over 71.")
        self.assertEqual(data["code"], 0)

    def test_tomorrow_from_the_fixture(self):
        text, data = weather_mod.describe(self.payload, day="tomorrow")
        self.assertEqual(text, "Tomorrow: thunderstorms, 89 over 69.")

    def test_the_sentence_fits_the_speech_cap(self):
        from avi_bot.brain import speech
        for day in ("today", "tomorrow"):
            text, _ = weather_mod.describe(self.payload, day=day)
            self.assertLessEqual(len(text.split()), speech.MAX_WORDS)

    def test_an_unknown_code_does_not_crash(self):
        payload = json.loads(json.dumps(self.payload))
        payload["daily"]["weather_code"] = [12345, 0]
        text, data = weather_mod.describe(payload)
        self.assertIn("unsettled", text)

    def test_a_missing_day_returns_nothing(self):
        text, data = weather_mod.describe(self.payload, day="next week")
        self.assertIsNotNone(text)       # falls back to today
        payload = {"daily": {"weather_code": [], "temperature_2m_max": []}}
        self.assertEqual(weather_mod.describe(payload), (None, None))

    def test_junk_payload_returns_nothing(self):
        self.assertEqual(weather_mod.describe({}), (None, None))
        self.assertEqual(weather_mod.describe(None), (None, None))

    def test_fetch_never_raises(self):
        """Weather is never worth an exception reaching the arm loop."""
        def boom(u, t):
            raise OSError("no network")
        self.assertEqual(weather_mod.fetch(opener=boom), (None, None))

    def test_fetch_uses_the_parser(self):
        raw = json.dumps(self.payload).encode()
        text, _ = weather_mod.fetch(opener=lambda u, t: raw)
        self.assertEqual(text, "Today: clear, 98 over 71.")

    def test_the_url_asks_for_phoenix_in_fahrenheit(self):
        u = weather_mod.url()
        self.assertIn("latitude=33.4484", u)
        self.assertIn("fahrenheit", u)
        self.assertIn("America%2FPhoenix", u)


# --- find: "a seeded database gives the right answer and the right yaw" --
class Find(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            "CREATE TABLE sightings (id INTEGER PRIMARY KEY, ts REAL,"
            " label TEXT, zone TEXT, cx REAL);")
        self.addCleanup(self.db.close)
        self.f = find_mod.Finder(self.db, yaw_deg_per_frame=83.9,
                                 frame_width=1280, centre_yaw=90.0,
                                 clock=self.clock)

    def seed(self, label, zone, cx, ago=60.0):
        self.db.execute(
            "INSERT INTO sightings (ts, label, zone, cx) VALUES (?,?,?,?)",
            (self.clock() - ago, label, zone, cx))
        self.db.commit()

    def test_a_synonym_finds_the_detector_label(self):
        """He says phone; the detector says cell phone."""
        self.seed("cell phone", "left of the keyboard", 400)
        text, yaw, found = self.f.find("phone")
        self.assertTrue(found)
        self.assertIn("left of the keyboard", text.lower())

    def test_the_yaw_centres_the_sighting(self):
        self.seed("mug", "right side", 1000)
        _, yaw, _ = self.f.find("mug")
        # cx 1000 of 1280 is +0.281 of a frame; raising yaw moves a landmark
        # LEFT, so something to the right needs MORE yaw.
        self.assertAlmostEqual(yaw, 90.0 + 0.28125 * 83.9, places=2)
        self.assertGreater(yaw, 90.0)

    def test_something_left_of_centre_needs_less_yaw(self):
        self.seed("keys", "left edge", 200)
        _, yaw, _ = self.f.find("keys")
        self.assertLess(yaw, 90.0)

    def test_dead_centre_needs_no_turn(self):
        self.seed("laptop", "middle", 640)
        _, yaw, _ = self.f.find("laptop")
        self.assertAlmostEqual(yaw, 90.0, places=6)

    def test_not_seen_today_admits_it_rather_than_guessing(self):
        """A confident wrong answer about where his keys are costs more than
        an admission."""
        self.seed("keys", "the desk", 500, ago=3 * 86400)
        text, yaw, found = self.f.find("keys")
        self.assertFalse(found)
        self.assertIsNone(yaw)
        self.assertIn("Haven't seen it", text)

    def test_never_seen_at_all(self):
        text, yaw, found = self.f.find("ocarina")
        self.assertFalse(found)
        self.assertIsNone(yaw)

    def test_the_most_recent_sighting_wins(self):
        self.seed("mug", "old spot", 200, ago=3600)
        self.seed("mug", "new spot", 900, ago=10)
        text, _, _ = self.f.find("mug")
        self.assertIn("new spot", text.lower())

    def test_canonical_is_idempotent(self):
        self.assertEqual(find_mod.canonical("phone"), "cell phone")
        self.assertEqual(find_mod.canonical("cell phone"), "cell phone")
        self.assertEqual(find_mod.canonical("  PHONE  "), "cell phone")

    def test_the_sentence_fits_the_speech_cap(self):
        from avi_bot.brain import speech
        self.seed("cell phone", "left of the keyboard", 400)
        text, _, _ = self.f.find("phone")
        self.assertLessEqual(len(text.split()), speech.MAX_WORDS)


if __name__ == "__main__":
    unittest.main()
