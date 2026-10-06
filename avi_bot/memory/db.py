"""The tables AVI remembers things in. BUILD_SPEC 10.

Added alongside the existing logger tables (observations, sightings,
sessions), not instead of them: there are already real rows in desk_memory.db
from weeks of running, and a schema that orphans them throws away the only
long-run data this project has.

THE LIMITS ARE PART OF THE DESIGN
`facts` caps at 100 rows of 280 characters (the pattern from Reachy Mini's
memory.py). Both numbers are deliberate. A fact store with no ceiling becomes
a transcript, a transcript cannot be put in a prompt, and a robot that
remembers everything about someone is a different and worse product than one
that remembers a hundred things. When it is full the OLDEST unused fact goes,
not the oldest fact, so something mentioned constantly survives.
"""
import sqlite3
import time
from datetime import date

MAX_FACTS = 100
MAX_FACT_CHARS = 280

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id         INTEGER PRIMARY KEY,
  text       TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  source     TEXT NOT NULL DEFAULT 'him',
  used_at    REAL,
  uses       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
  id      INTEGER PRIMARY KEY,
  due_ts  REAL NOT NULL,
  text    TEXT NOT NULL,
  kind    TEXT NOT NULL DEFAULT 'reminder',
  status  TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(due_ts, status);
CREATE TABLE IF NOT EXISTS interactions (
  id         INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  utterance  TEXT,
  route      TEXT,
  provider   TEXT,
  tools      TEXT,
  latency_ms REAL,
  outcome    TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
  id         INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  suggestion TEXT NOT NULL,
  response   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mood_log (
  id      INTEGER PRIMARY KEY,
  ts      REAL NOT NULL,
  valence REAL, arousal REAL, social REAL,
  emotion TEXT
);
CREATE TABLE IF NOT EXISTS provider_usage (
  day      TEXT NOT NULL,
  provider TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, provider)
);
"""


class Store:
    def __init__(self, path=":memory:", clock=time.time):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.clock = clock

    # --- facts -----------------------------------------------------------
    def remember(self, text, source="him"):
        """Store a fact. Returns (id, dropped_text_or_None)."""
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("a fact needs text")
        if len(text) > MAX_FACT_CHARS:
            raise ValueError(f"facts cap at {MAX_FACT_CHARS} characters; "
                             f"this one is {len(text)}")
        existing = self.db.execute("SELECT id FROM facts WHERE text=?",
                                   (text,)).fetchone()
        if existing:
            return existing["id"], None
        dropped = None
        if self.count_facts() >= MAX_FACTS:
            # Oldest UNUSED, not oldest: something he brings up constantly is
            # the last thing to forget.
            row = self.db.execute(
                "SELECT id, text FROM facts"
                " ORDER BY uses ASC, COALESCE(used_at, created_at) ASC"
                " LIMIT 1").fetchone()
            if row:
                dropped = row["text"]
                self.db.execute("DELETE FROM facts WHERE id=?", (row["id"],))
        cur = self.db.execute(
            "INSERT INTO facts (text, created_at, source) VALUES (?,?,?)",
            (text, self.clock(), source))
        self.db.commit()
        return cur.lastrowid, dropped

    def forget(self, text):
        text = " ".join((text or "").split())
        cur = self.db.execute("DELETE FROM facts WHERE text=?", (text,))
        self.db.commit()
        return cur.rowcount

    def count_facts(self):
        return self.db.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]

    def recall(self, query=None, limit=10):
        """Facts matching a query, most-used first. Marks them as used.

        The cap is 10 by default because BUILD_SPEC 8.2 allows "up to 10
        matching facts" into the prompt, and a recall that returns more than
        the prompt can carry just moves the truncation somewhere less careful.
        """
        if query:
            words = [w for w in query.lower().split() if len(w) > 2]
            if words:
                clause = " OR ".join("LOWER(text) LIKE ?" for _ in words)
                rows = self.db.execute(
                    f"SELECT * FROM facts WHERE {clause}"
                    f" ORDER BY uses DESC, created_at DESC LIMIT ?",
                    [f"%{w}%" for w in words] + [limit]).fetchall()
            else:
                rows = []
        else:
            rows = self.db.execute(
                "SELECT * FROM facts ORDER BY uses DESC, created_at DESC"
                " LIMIT ?", (limit,)).fetchall()
        now = self.clock()
        for r in rows:
            self.db.execute(
                "UPDATE facts SET uses = uses + 1, used_at = ? WHERE id = ?",
                (now, r["id"]))
        self.db.commit()
        return [r["text"] for r in rows]

    # --- reminders -------------------------------------------------------
    def add_reminder(self, due_ts, text, kind="reminder"):
        cur = self.db.execute(
            "INSERT INTO reminders (due_ts, text, kind) VALUES (?,?,?)",
            (float(due_ts), text, kind))
        self.db.commit()
        return cur.lastrowid

    def due_reminders(self, now=None):
        """Pending reminders whose time has come. Does NOT mark them done:
        the caller marks them after it has actually spoken, so a crash between
        the two loses a reminder rather than silently eating it."""
        now = self.clock() if now is None else now
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM reminders WHERE status='pending' AND due_ts <= ?"
            " ORDER BY due_ts", (now,))]

    def mark_reminder(self, rid, status="done"):
        self.db.execute("UPDATE reminders SET status=? WHERE id=?",
                        (status, rid))
        self.db.commit()

    # --- logs ------------------------------------------------------------
    def log_interaction(self, utterance, route, provider=None, tools=None,
                        latency_ms=None, outcome=None):
        self.db.execute(
            "INSERT INTO interactions (ts, utterance, route, provider, tools,"
            " latency_ms, outcome) VALUES (?,?,?,?,?,?,?)",
            (self.clock(), utterance, route, provider,
             ",".join(tools) if tools else None, latency_ms, outcome))
        self.db.commit()

    def log_mood(self, valence, arousal, social, emotion):
        self.db.execute(
            "INSERT INTO mood_log (ts, valence, arousal, social, emotion)"
            " VALUES (?,?,?,?,?)",
            (self.clock(), valence, arousal, social, emotion))
        self.db.commit()

    def log_feedback(self, suggestion, response):
        self.db.execute(
            "INSERT INTO feedback (ts, suggestion, response) VALUES (?,?,?)",
            (self.clock(), suggestion, response))
        self.db.commit()

    def acceptance_rate(self, since_ts=None):
        """How often his answer to a suggestion was yes. BUILD_SPEC M6.

        Returns None rather than 0.0 when nothing has been suggested yet --
        'never asked' and 'always refused' are very different facts and a
        proactive policy that confuses them will either never speak or never
        stop.
        """
        where, args = "", []
        if since_ts is not None:
            where, args = " WHERE ts >= ?", [since_ts]
        rows = self.db.execute(
            f"SELECT response FROM feedback{where}", args).fetchall()
        if not rows:
            return None
        yes = sum(1 for r in rows if r["response"] in ("yes", "accepted"))
        return yes / len(rows)

    # --- provider usage --------------------------------------------------
    def note_provider(self, provider, failed=False, day=None):
        day = day or date.today().isoformat()
        self.db.execute(
            "INSERT INTO provider_usage (day, provider, requests, failures)"
            " VALUES (?,?,1,?) ON CONFLICT(day, provider) DO UPDATE SET"
            " requests = requests + 1, failures = failures + ?",
            (day, provider, int(failed), int(failed)))
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
