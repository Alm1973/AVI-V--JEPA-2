"""A sighting has to carry the pose it was taken from.

`zone` means "left of the camera view", and that is a place in the room only
if you also know which way the camera was facing. Nothing recorded that, so
find() assumed OBSERVE at base_yaw 90 forever -- and when OBSERVE was retaught
to 60, every answer would have been a third of a frame out while the sentence
stayed correct. That is the failure this project keeps paying for, so the
sighting now describes itself.

memory.py needs assistant/ on the path for its `settings` import; that file
is in the repo and loads with no GPU and no camera.
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_memory():
    """Import assistant/memory.py.

    No stub: assistant/settings.py is a real file in this repo and loads
    without a GPU or a camera. A hand-written stub was worse than nothing --
    it SHADOWED the real settings and then failed on the first constant it
    had not thought to define.
    """
    sys.path.insert(0, str(ROOT / "assistant"))
    import memory
    return memory


mem_mod = load_memory()

POSE = {"base_yaw": 60.0, "shoulder": 84.0, "elbow": 95.0, "wrist": 147.9}


def a_sighting(label="mug", x=0.45, w=0.10, y=0.45, h=0.10):
    return {"label": label, "det_conf": 0.8, "vlm_margin": 0.5,
            "x": x, "y": y, "w": w, "h": h}


def store():
    m = mem_mod.Memory(":memory:")
    return m


def add(m, pose=None, **kw):
    return m.add_observation(
        ts=1_791_295_200.0, trigger="heartbeat", activity="working",
        activity_raw="working", activity_p=0.9, probs={"working": 0.9},
        person_conf=0.9, vlm_calls=0, latency_ms=1.0,
        sightings=[a_sighting(**kw)], pose=pose)


class TheColumnsExist(unittest.TestCase):
    def test_a_fresh_database_has_the_joint_columns(self):
        m = store()
        self.addCleanup(m.close)
        have = {r["name"] for r in m.db.execute("PRAGMA table_info(sightings)")}
        for col in mem_mod.JOINT_ORDER:
            self.assertIn(col, have)


class Migration(unittest.TestCase):
    """CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
    so a schema change is invisible to every database that predates it -- and
    the real one has months of observations in it."""

    def old_schema_db(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(
            "CREATE TABLE sightings (id INTEGER PRIMARY KEY,"
            " observation_id INTEGER, ts REAL, label TEXT, det_conf REAL,"
            " vlm_margin REAL, x REAL, y REAL, w REAL, h REAL, zone TEXT);")
        self.addCleanup(db.close)
        return db

    def test_it_adds_what_is_missing(self):
        db = self.old_schema_db()
        added = mem_mod.migrate(db)
        self.assertEqual(sorted(added), sorted(mem_mod.JOINT_ORDER))
        have = {r["name"] for r in db.execute("PRAGMA table_info(sightings)")}
        for col in mem_mod.JOINT_ORDER:
            self.assertIn(col, have)

    def test_it_is_idempotent(self):
        db = self.old_schema_db()
        mem_mod.migrate(db)
        self.assertEqual(mem_mod.migrate(db), [],
                         "a second run tried to add columns again")

    def test_it_keeps_the_existing_rows(self):
        """A migration that loses months of observations is not a migration."""
        db = self.old_schema_db()
        db.execute(
            "INSERT INTO sightings (ts, label, det_conf, vlm_margin, x, y,"
            " w, h, zone) VALUES (1,'mug',0.8,0.5,0.4,0.4,0.1,0.1,'middle')")
        db.commit()
        mem_mod.migrate(db)
        row = db.execute("SELECT * FROM sightings").fetchone()
        self.assertEqual(row["label"], "mug")
        self.assertIsNone(row["base_yaw"], "an old row has no pose to claim")


class WhatGetsWritten(unittest.TestCase):
    def test_a_pose_is_recorded_when_one_is_given(self):
        m = store()
        self.addCleanup(m.close)
        add(m, pose=POSE)
        row = m.db.execute("SELECT * FROM sightings").fetchone()
        for j, v in POSE.items():
            self.assertAlmostEqual(row[j], v, places=4)

    def test_no_pose_writes_nulls_rather_than_a_guess(self):
        m = store()
        self.addCleanup(m.close)
        add(m, pose=None)
        row = m.db.execute("SELECT * FROM sightings").fetchone()
        for j in mem_mod.JOINT_ORDER:
            self.assertIsNone(row[j])

    def test_a_partial_pose_is_no_pose(self):
        """Three of four angles still leaves the camera's aim undetermined,
        and a half-known pose that reads as an answer is worse than none."""
        m = store()
        self.addCleanup(m.close)
        add(m, pose={"base_yaw": 60.0, "wrist": 147.9})
        row = m.db.execute("SELECT * FROM sightings").fetchone()
        for j in mem_mod.JOINT_ORDER:
            self.assertIsNone(row[j], f"{j} was written from a partial pose")

    def test_a_junk_pose_does_not_lose_the_observation(self):
        m = store()
        self.addCleanup(m.close)
        obs = add(m, pose={"base_yaw": "sideways", "shoulder": 1,
                           "elbow": 1, "wrist": 1})
        self.assertIsNotNone(obs)
        row = m.db.execute("SELECT * FROM sightings").fetchone()
        self.assertEqual(row["label"], "mug")
        self.assertIsNone(row["base_yaw"])

    def test_the_zone_still_comes_from_the_box_centre(self):
        m = store()
        self.addCleanup(m.close)
        add(m, pose=POSE, x=0.70, w=0.10)      # centre 0.75 -> right third
        row = m.db.execute("SELECT * FROM sightings").fetchone()
        self.assertEqual(row["zone"], "right")


class FindUsesTheRecordedPose(unittest.TestCase):
    """The point of the whole change: an old answer must not move when a pose
    is retaught."""

    def setUp(self):
        from avi_bot.skills import find as find_mod
        self.find_mod = find_mod
        self.m = store()
        self.addCleanup(self.m.close)

    def finder(self, centre_yaw=None, centre_wrist=None):
        return self.find_mod.Finder(
            self.m.db, 83.9, frame_width=1280, frame_height=720,
            wrist_deg_per_frame=32.2, centre_yaw=centre_yaw,
            centre_wrist=centre_wrist, clock=lambda: 1_791_295_200.0 + 30)

    def test_the_rows_own_pose_is_used(self):
        add(self.m, pose=POSE, x=0.70, w=0.10)         # centre 0.75
        aim = self.finder().aim("mug")                 # no centre supplied
        self.assertTrue(aim["confident"])
        self.assertTrue(aim["pose_recorded"])
        self.assertAlmostEqual(aim["base_yaw"], 60.0 + 0.25 * 83.9, places=2)

    def test_it_beats_a_different_assumed_centre(self):
        """A sighting taken at base_yaw 60 stays at 60 even if the caller
        believes the camera now sits at 90."""
        add(self.m, pose=POSE, x=0.45, w=0.10)         # dead centre
        aim = self.finder(centre_yaw=90.0, centre_wrist=120.0).aim("mug")
        self.assertAlmostEqual(aim["base_yaw"], 60.0, places=4)
        self.assertAlmostEqual(aim["wrist"], 147.9, places=4)

    def test_the_shape_it_was_taken_at_comes_back(self):
        """Shoulder and elbow are reported so a caller can tell whether the
        arm is still in the shape that makes the angles mean anything."""
        add(self.m, pose=POSE)
        aim = self.finder().aim("mug")
        self.assertEqual(aim["shape"], {"shoulder": 84.0, "elbow": 95.0})

    def test_a_poseless_row_still_refuses_to_guess(self):
        add(self.m, pose=None, x=0.70, w=0.10)
        aim = self.finder().aim("mug")
        self.assertFalse(aim["confident"])
        self.assertIsNone(aim["base_yaw"])

    def test_a_poseless_row_can_still_use_a_supplied_centre(self):
        """Old rows predate the columns, and OBSERVE is still the right guess
        for them -- it just has to be passed in deliberately."""
        add(self.m, pose=None, x=0.70, w=0.10)
        aim = self.finder(centre_yaw=60.0, centre_wrist=147.9).aim("mug")
        self.assertTrue(aim["confident"])
        self.assertFalse(aim["pose_recorded"])
        self.assertAlmostEqual(aim["base_yaw"], 60.0 + 0.25 * 83.9, places=2)
