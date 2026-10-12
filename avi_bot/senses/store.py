"""Where the senses write: events, a time series, raw vectors, learned state.

Stored per the launch-day rule (presence.py): senses-dev.db by default, so what
is gathered during development never becomes part of what AVI knows about him
on the day it is born. `--profile live` writes senses.db.

No image is ever stored. `vectors` holds numbers MediaPipe derived from a frame
(hand points, face expression weights), which the later self-learned models
cluster: they describe him, so they stay on the MSI like everything else.
"""
import json
import sqlite3
import struct
import time

PROFILES = {"dev": "senses-dev.db", "live": "senses.db"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts     REAL NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT                       -- JSON, may be NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS samples (
    ts           REAL NOT NULL,
    present      INTEGER NOT NULL,
    seen_frac    REAL, lying_frac REAL, looking_frac REAL, hands_frac REAL,
    faces_max    INTEGER,
    brightness   REAL,
    lights_on    INTEGER,
    decoy_hits   INTEGER
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS vectors (
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL,              -- hand | face_blend | pose
    data  BLOB NOT NULL               -- little-endian float32
);
CREATE INDEX IF NOT EXISTS vectors_kind_ts ON vectors(kind, ts);
CREATE TABLE IF NOT EXISTS learned (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,              -- JSON
    at    REAL NOT NULL
);
"""

SAMPLE_COLS = ("ts", "present", "seen_frac", "lying_frac", "looking_frac",
               "hands_frac", "faces_max", "brightness", "lights_on", "decoy_hits")


def pack(values):
    return struct.pack(f"<{len(values)}f", *values)


def unpack(blob):
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class Store:
    def __init__(self, path, clock=time.time):
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.clock = clock

    def event(self, ev):
        self.db.execute("INSERT INTO events (ts, kind, detail) VALUES (?,?,?)",
                        (ev.ts, ev.kind, json.dumps(ev.detail) if ev.detail else None))

    def sample(self, row):
        self.db.execute(
            f"INSERT INTO samples ({','.join(SAMPLE_COLS)}) VALUES ({','.join('?' * len(SAMPLE_COLS))})",
            tuple(row.get(c) for c in SAMPLE_COLS))

    def vector(self, ts, kind, values):
        self.db.execute("INSERT INTO vectors (ts, kind, data) VALUES (?,?,?)",
                        (ts, kind, pack(values)))

    def commit(self):
        self.db.commit()

    def save(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO learned (key, value, at) VALUES (?,?,?)",
                        (key, json.dumps(value), self.clock()))
        self.db.commit()

    def load(self, key, default=None):
        r = self.db.execute("SELECT value FROM learned WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def events(self, since=0.0, kind=None):
        q, args = "SELECT ts, kind, detail FROM events WHERE ts >= ?", [float(since)]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        return [(ts, k, json.loads(d) if d else {})
                for ts, k, d in self.db.execute(q + " ORDER BY ts", args)]

    def counts(self, since=0.0):
        """Rows per table since `since`: the quick 'is it logging?' check."""
        out = {}
        for t in ("events", "samples", "vectors"):
            out[t] = self.db.execute(f"SELECT COUNT(*) FROM {t} WHERE ts >= ?",
                                     (float(since),)).fetchone()[0]
        return out

    def close(self):
        self.db.commit()
        self.db.close()
