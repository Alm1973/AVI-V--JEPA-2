"""SQLite memory: observations, object sightings and activity sessions.

Only text and numbers are stored. No images.
"""
import json
import sqlite3
from datetime import datetime

import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id           INTEGER PRIMARY KEY,
    ts           REAL NOT NULL,          -- unix time
    local_time   TEXT NOT NULL,          -- e.g. 2026-10-02T16:12:05-07:00
    local_date   TEXT NOT NULL,          -- e.g. 2026-10-02
    weekday      INTEGER NOT NULL,       -- 0 = Monday
    trigger      TEXT NOT NULL,          -- heartbeat | change | change-forced | startup
    activity     TEXT NOT NULL,          -- a label from settings.ACTIVITIES, or "uncertain"
    activity_raw TEXT NOT NULL,          -- the VLM's top label before the confidence check
    activity_p   REAL NOT NULL,          -- probability of activity_raw
    probs        TEXT NOT NULL,          -- JSON {label: probability}
    person_conf  REAL,                   -- detector's best "person" score, for later analysis
    vlm_calls    INTEGER NOT NULL,
    latency_ms   REAL NOT NULL,
    audit_path   TEXT                    -- thumbnail path when audit mode saved one
);
CREATE INDEX IF NOT EXISTS obs_date ON observations(local_date, ts);

CREATE TABLE IF NOT EXISTS sightings (
    id             INTEGER PRIMARY KEY,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    ts             REAL NOT NULL,
    label          TEXT NOT NULL,
    det_conf       REAL NOT NULL,
    vlm_margin     REAL NOT NULL,        -- yes score minus no score; > 0 means verified
    x REAL, y REAL, w REAL, h REAL,      -- box as fractions of the frame
    zone           TEXT NOT NULL         -- left | middle | right of the camera view
);
CREATE INDEX IF NOT EXISTS sight_label ON sightings(label, ts);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    local_date TEXT NOT NULL,
    activity   TEXT NOT NULL,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    minutes    REAL NOT NULL,
    n_obs      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sess_date ON sessions(local_date, start_ts);
"""


def local_parts(ts):
    dt = datetime.fromtimestamp(ts).astimezone()
    return dt.isoformat(timespec="seconds"), dt.date().isoformat(), dt.weekday()


def zone_for(x_center_frac):
    if x_center_frac < 1 / 3:
        return "left"
    if x_center_frac > 2 / 3:
        return "right"
    return "middle"


class Memory:
    def __init__(self, path=settings.DB_PATH):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def add_observation(self, ts, trigger, activity, activity_raw, activity_p,
                        probs, person_conf, vlm_calls, latency_ms,
                        sightings=(), audit_path=None):
        local_time, local_date, weekday = local_parts(ts)
        cur = self.db.execute(
            "INSERT INTO observations (ts, local_time, local_date, weekday, trigger,"
            " activity, activity_raw, activity_p, probs, person_conf, vlm_calls,"
            " latency_ms, audit_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, local_time, local_date, weekday, trigger, activity, activity_raw,
             activity_p, json.dumps({k: round(v, 4) for k, v in probs.items()}),
             person_conf, vlm_calls, latency_ms, audit_path))
        obs_id = cur.lastrowid
        for s in sightings:
            self.db.execute(
                "INSERT INTO sightings (observation_id, ts, label, det_conf,"
                " vlm_margin, x, y, w, h, zone) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (obs_id, ts, s["label"], s["det_conf"], s["vlm_margin"],
                 s["x"], s["y"], s["w"], s["h"], zone_for(s["x"] + s["w"] / 2)))
        self.db.commit()
        return obs_id

    def set_audit_path(self, obs_id, path):
        self.db.execute("UPDATE observations SET audit_path = ? WHERE id = ?",
                        (path, obs_id))
        self.db.commit()

    def observations(self, local_date):
        return self.db.execute(
            "SELECT * FROM observations WHERE local_date = ? ORDER BY ts",
            (local_date,)).fetchall()

    def rebuild_sessions(self, local_date,
                         max_gap_s=settings.SESSION_MAX_GAP_S,
                         min_minutes=settings.SESSION_MIN_MINUTES,
                         tail_s=settings.HEARTBEAT_S):
        """Merge one day's observations into activity sessions.

        Same-label observations closer than max_gap_s join one session
        ("not at desk" uses the slower away heartbeat instead).
        "uncertain" observations never start or break a session, and a run of
        them bridges the gap between two same-label observations.
        A session runs until the next observation (capped at one heartbeat)
        after its last matching observation. Sessions shorter than min_minutes
        are dropped.
        """
        rows = self.observations(local_date)
        sessions = []
        cur = None

        def limits(activity):
            if activity == settings.NOBODY:
                return settings.AWAY_HEARTBEAT_S * 1.5, settings.AWAY_HEARTBEAT_S
            return max_gap_s, tail_s

        for i, r in enumerate(rows):
            if r["activity"] == settings.UNCERTAIN:
                if cur is not None and r["ts"] - cur["reach_ts"] <= limits(cur["activity"])[0]:
                    cur["reach_ts"] = r["ts"]      # bridge, without counting it
                continue
            gap_limit, tail_limit = limits(r["activity"])
            same = (cur is not None and r["activity"] == cur["activity"]
                    and r["ts"] - cur["reach_ts"] <= gap_limit)
            if not same:
                if cur is not None:
                    sessions.append(cur)
                cur = {"activity": r["activity"], "start_ts": r["ts"], "n_obs": 0}
            cur["reach_ts"] = r["ts"]
            cur["n_obs"] += 1
            next_ts = rows[i + 1]["ts"] if i + 1 < len(rows) else None
            tail = tail_limit if next_ts is None else min(tail_limit, next_ts - r["ts"])
            cur["end_ts"] = r["ts"] + max(0.0, tail)
        if cur is not None:
            sessions.append(cur)
        for s in sessions:
            s.pop("reach_ts", None)

        kept = [s for s in sessions
                if (s["end_ts"] - s["start_ts"]) / 60 >= min_minutes]
        self.db.execute("DELETE FROM sessions WHERE local_date = ?", (local_date,))
        for s in kept:
            self.db.execute(
                "INSERT INTO sessions (local_date, activity, start_ts, end_ts,"
                " minutes, n_obs) VALUES (?,?,?,?,?,?)",
                (local_date, s["activity"], s["start_ts"], s["end_ts"],
                 round((s["end_ts"] - s["start_ts"]) / 60, 1), s["n_obs"]))
        self.db.commit()
        return kept

    def sessions(self, local_date):
        return self.db.execute(
            "SELECT * FROM sessions WHERE local_date = ? ORDER BY start_ts",
            (local_date,)).fetchall()

    def last_seen(self, label):
        """Most recent verified sighting of an object, or None."""
        return self.db.execute(
            "SELECT * FROM sightings WHERE label = ? AND vlm_margin > 0"
            " ORDER BY ts DESC LIMIT 1", (label,)).fetchone()

    def last_seen_all(self):
        return self.db.execute(
            "SELECT label, MAX(ts) AS ts, zone FROM sightings WHERE vlm_margin > 0"
            " GROUP BY label ORDER BY ts DESC").fetchall()

    def forget_day(self, local_date):
        """Delete a day's observations, sightings, sessions and audit thumbnails."""
        import os
        for r in self.db.execute(
                "SELECT audit_path FROM observations WHERE local_date = ?"
                " AND audit_path IS NOT NULL", (local_date,)):
            try:
                os.remove(r["audit_path"])
            except OSError:
                pass
        self.db.execute("DELETE FROM observations WHERE local_date = ?", (local_date,))
        self.db.execute("DELETE FROM sessions WHERE local_date = ?", (local_date,))
        self.db.commit()
