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

from avi_bot.memory import locked

MAX_FACTS = 100
MAX_FACT_CHARS = 280

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id         INTEGER PRIMARY KEY,
  text       TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  source     TEXT NOT NULL DEFAULT 'him',
  used_at    REAL,
  uses       INTEGER NOT NULL DEFAULT 0,
  -- Retrieval and usefulness are NOT the same thing, and conflating them is
  -- why a fact store fills with trivia. `uses` counts times retrieved;
  -- `cites` counts times the reply actually drew on it. Reflective Memory
  -- Management (arXiv 2503.08026) reports >10% on exactly this distinction:
  -- refine RETRIEVAL from what was cited, not from what was fetched.
  cites      INTEGER NOT NULL DEFAULT 0,
  cited_at   REAL
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
-- Performances AVI made up itself ("dance like a dolphin"), kept so the
-- same request gets the same dance, and changed by how it went: scaled up
-- when the camera saw nothing move, dropped when he disliked it.
CREATE TABLE IF NOT EXISTS learned_moves (
  key        TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  idea       TEXT,
  moves      TEXT NOT NULL,
  source     TEXT NOT NULL,
  created_at REAL NOT NULL,
  performed  INTEGER NOT NULL DEFAULT 0,
  last_ts    REAL,
  score      INTEGER NOT NULL DEFAULT 0,
  scale      REAL NOT NULL DEFAULT 1.0,
  seen       INTEGER
);
-- His words and the actions they came to mean (brain/shortcuts.py).
CREATE TABLE IF NOT EXISTS shortcuts (
  key        TEXT PRIMARY KEY,
  calls      TEXT NOT NULL,
  seen       INTEGER NOT NULL DEFAULT 1,
  proven     INTEGER NOT NULL DEFAULT 0,
  uses       INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  last_ts    REAL
);
-- Small things that must survive a restart ("the heads-up was given today").
CREATE TABLE IF NOT EXISTS flags (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS provider_usage (
  day      TEXT NOT NULL,
  provider TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, provider)
);
"""


_FILLER = {"a", "an", "the", "on", "is", "his", "he", "has", "at", "for",
           "this", "shaurya", "shaurya's", "my", "i", "have"}


def _same(text):
    """A fact's identity: lower case, punctuation and filler words gone."""
    words = "".join(c if c.isalnum() or c in " '" else " "
                    for c in text.lower()).split()
    return " ".join(w for w in words if w not in _FILLER)


class Store:
    def __init__(self, path=":memory:", clock=time.time):
        # Hosted in the dashboard, each HTTP request runs on its own thread
        # and the scheduler ticks on another; the default per-thread check
        # failed every turn after the first. Turns are serialised by the
        # host (dashboard/brain_host.py), so sharing the connection is safe.
        self.db = locked.connect(path)
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
        # Check, evict, insert as one step: two threads remembering at once
        # both saw room and the store went past its cap (test_locked_db).
        with self.db.lock:
            existing = self.db.execute("SELECT id FROM facts WHERE text=?",
                                       (text,)).fetchone()
            if existing:
                return existing["id"], None
            # The same fact in other words is not a new fact: once AVI
            # remembers what he mentions in passing, "Math test on Monday."
            # and "math test Monday" both arrive. A full scan is fine at
            # MAX_FACTS rows.
            key = _same(text)
            for row in self.db.execute("SELECT id, text FROM facts"):
                if _same(row["text"]) == key:
                    return row["id"], None
            dropped = None
            if self.count_facts() >= MAX_FACTS:
                # Oldest UNUSED, not oldest: something he brings up
                # constantly is the last thing to forget.
                row = self.db.execute(
                    "SELECT id, text FROM facts"
                    " ORDER BY cites ASC, uses ASC,"
                    " COALESCE(used_at, created_at) ASC LIMIT 1").fetchone()
                if row:
                    dropped = row["text"]
                    self.db.execute("DELETE FROM facts WHERE id=?",
                                    (row["id"],))
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
        # Ranked by CITATIONS first. A fact retrieved ten times and never
        # drawn on is worse than one cited once: it has been crowding the
        # prompt for free.
        order = "cites DESC, uses DESC, created_at DESC"
        if query:
            words = [w for w in query.lower().split() if len(w) > 2]
            if words:
                clause = " OR ".join("LOWER(text) LIKE ?" for _ in words)
                rows = self.db.execute(
                    f"SELECT * FROM facts WHERE {clause}"
                    f" ORDER BY {order} LIMIT ?",
                    [f"%{w}%" for w in words] + [limit]).fetchall()
            else:
                rows = []
        else:
            rows = self.db.execute(
                f"SELECT * FROM facts ORDER BY {order} LIMIT ?",
                (limit,)).fetchall()
        now = self.clock()
        for r in rows:
            self.db.execute(
                "UPDATE facts SET uses = uses + 1, used_at = ? WHERE id = ?",
                (now, r["id"]))
        self.db.commit()
        return [r["text"] for r in rows]

    def cite(self, texts):
        """Mark the facts a reply actually drew on. Returns how many matched.

        Called AFTER the reply exists, which is the only time this is knowable.
        Retrieval is a guess about relevance; a citation is evidence of it, and
        only evidence should steer what reaches the prompt next time.
        """
        if isinstance(texts, str):
            texts = [texts]
        now = self.clock()
        n = 0
        for text in texts or []:
            text = " ".join((text or "").split())
            if not text:
                continue
            cur = self.db.execute(
                "UPDATE facts SET cites = cites + 1, cited_at = ?"
                " WHERE text = ?", (now, text))
            n += cur.rowcount
        self.db.commit()
        return n

    def facts(self, limit=200):
        """Every fact with when it was said, newest first. Read-only:
        unlike recall(), looking does not count as using."""
        return [{"text": r["text"], "created_at": r["created_at"]}
                for r in self.db.execute(
                    "SELECT text, created_at FROM facts"
                    " ORDER BY created_at DESC LIMIT ?", (int(limit),))]

    def dead_weight(self, min_uses=3):
        """Facts retrieved repeatedly and never once cited.

        These are the ones to drop first when the store is full: they are
        actively costing prompt budget and have never earned it.
        """
        return [r["text"] for r in self.db.execute(
            "SELECT text FROM facts WHERE cites = 0 AND uses >= ?"
            " ORDER BY uses DESC", (min_uses,))]

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

    def reminders(self, status):
        """Reminders in one status (e.g. "missed"), oldest due first."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM reminders WHERE status=? ORDER BY due_ts",
            (status,))]

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

    # --- flags ----------------------------------------------------------
    def get_flag(self, key):
        r = self.db.execute("SELECT value FROM flags WHERE key=?",
                            (key,)).fetchone()
        return r[0] if r else None

    def set_flag(self, key, value):
        self.db.execute("INSERT INTO flags (key, value) VALUES (?,?) ON "
                        "CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, None if value is None else str(value)))
        self.db.commit()

    # --- performances AVI composed -----------------------------------------
    # Words that say "a performance" rather than which one: "do an
    # interpretive dance like a dolphin" and "dolphin dance" are one dance.
    _MOVE_FILLER = {"a", "an", "the", "like", "do", "your", "my", "of", "me",
                    "dance", "dancing", "interpretive", "interpretative",
                    "interprative", "move", "moves", "impression", "some",
                    "routine", "little", "please", "avi", "can", "you",
                    "again", "show", "perform", "that", "this"}
    MAX_MOVES_KEPT = 50

    @classmethod
    def move_key(cls, name):
        words = "".join(c if c.isalnum() else " "
                        for c in (name or "").lower()).split()
        kept = [w for w in words if w not in cls._MOVE_FILLER]
        return " ".join(kept) or "dance"

    def save_move(self, name, moves, idea=None, source="composed"):
        """Keep a performance under its name. A new one for a known name
        replaces it and starts its record over: it is a different dance."""
        import json
        key = self.move_key(name)
        with self.db.lock:
            self.db.execute(
                "INSERT INTO learned_moves (key, name, idea, moves, source,"
                " created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE"
                " SET name=excluded.name, idea=excluded.idea,"
                " moves=excluded.moves, source=excluded.source,"
                " created_at=excluded.created_at, score=0, scale=1.0,"
                " seen=NULL",
                (key, " ".join((name or key).split())[:60], idea,
                 json.dumps(moves), source, self.clock()))
            n = self.db.execute(
                "SELECT COUNT(*) FROM learned_moves").fetchone()[0]
            if n > self.MAX_MOVES_KEPT:
                self.db.execute(
                    "DELETE FROM learned_moves WHERE key IN (SELECT key FROM"
                    " learned_moves ORDER BY score ASC, COALESCE(last_ts,"
                    " created_at) ASC LIMIT ?", (n - self.MAX_MOVES_KEPT,))
            self.db.commit()
        return key

    def get_move(self, name):
        import json
        r = self.db.execute("SELECT * FROM learned_moves WHERE key=?",
                            (self.move_key(name),)).fetchone()
        if r is None:
            return None
        out = dict(r)
        out["moves"] = json.loads(out["moves"])
        return out

    def drop_move(self, name):
        cur = self.db.execute("DELETE FROM learned_moves WHERE key=?",
                              (self.move_key(name),))
        self.db.commit()
        return cur.rowcount

    def moves_known(self, limit=8):
        """[{name, score, performed}], best liked first."""
        return [dict(r) for r in self.db.execute(
            "SELECT name, score, performed FROM learned_moves"
            " ORDER BY score DESC, performed DESC, created_at DESC LIMIT ?",
            (int(limit),))]

    # The camera seeing nothing happen costs the dance this much size next
    # time, up to MAX_SCALE: on this arm, small moves vanish into backlash.
    GROW = 1.4
    MAX_SCALE = 2.0

    def performed_move(self, name, seen=None):
        """Count a performance. seen=False (the camera saw no motion)
        makes the next one bigger; None (could not check) changes nothing."""
        key = self.move_key(name)
        self.db.execute(
            "UPDATE learned_moves SET performed = performed + 1, last_ts = ?,"
            " seen = ?, scale = CASE WHEN ? = 0 THEN MIN(?, scale * ?)"
            " ELSE scale END WHERE key = ?",
            (self.clock(), None if seen is None else int(bool(seen)),
             None if seen is None else int(bool(seen)), self.MAX_SCALE,
             self.GROW, key))
        self.db.commit()

    def last_move(self):
        r = self.db.execute(
            "SELECT name, last_ts FROM learned_moves WHERE last_ts IS NOT"
            " NULL ORDER BY last_ts DESC LIMIT 1").fetchone()
        return (r["name"], r["last_ts"]) if r else (None, None)

    def rate_move(self, name, delta):
        cur = self.db.execute(
            "UPDATE learned_moves SET score = score + ? WHERE key = ?",
            (int(delta), self.move_key(name)))
        self.db.commit()
        return cur.rowcount

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
