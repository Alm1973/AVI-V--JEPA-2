"""When is Shaurya at the desk? Learned by watching, not by being told.

AVI's main directive is to learn about him by observing his surroundings and
help as much as it can. Presence is the first thing to learn, because almost
everything useful hangs off it: when he arrives, how long he stays, when he
usually sits down, how long it has been since a break.

Until this existed nothing learned it. "arrived" and "left" were mood events
fired by buttons on the dashboard page, and the observer that does record
person detections cannot run while the dashboard owns the camera.

Two rules carry the correctness.

  ABSENCE NEEDS A VIEW. Not seeing him while the camera points at the ceiling
  says nothing about whether he is there. Every sample records whether the arm
  was looking at his seat; only those can count as "away". A detection counts
  from anywhere -- if he is seen, he is there.

  HYSTERESIS. One frame with a person in it is not an arrival and one frame
  without is not a departure; detectors flicker. Arrival needs ARRIVE_AFTER
  consecutive sightings and departure LEAVE_AFTER consecutive valid absences,
  so a turned head or a missed frame does not end a session.

Stored per the launch-day rule: presence-dev.db by default, so what is learned
during development never becomes part of what AVI knows about him on the day it
is born.
"""
import sqlite3
import time
from datetime import datetime

from avi_bot.perception import habits

ARRIVE_AFTER = 2          # consecutive sightings before he counts as arrived
LEAVE_AFTER = 6           # consecutive VALID absences before he has left
MIN_SESSION_MIN = 3.0     # shorter spans are not reported as sessions
# A break offer is TAKEN if he left within this long of it. Departure is
# only recorded LEAVE_AFTER looks (2 min) after he actually goes, hence 7.
BREAK_TAKEN_WITHIN_S = 420.0

PROFILES = {"dev": "presence-dev.db", "live": "presence.db"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts       REAL NOT NULL,
    present  INTEGER NOT NULL,     -- a person was detected in this frame
    conf     REAL,                 -- the best person confidence, if any
    view_ok  INTEGER NOT NULL      -- the arm was looking at his seat
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS events (
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL            -- arrived | left | break_offered
);
"""


class Presence:
    def __init__(self, path=":memory:", clock=time.time,
                 arrive_after=ARRIVE_AFTER, leave_after=LEAVE_AFTER):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.clock = clock
        self.arrive_after = int(arrive_after)
        self.leave_after = int(leave_after)
        self.here = False
        self._seen_run = 0
        self._gone_run = 0
        self.since = None
        last = self.db.execute(
            "SELECT ts, kind FROM events ORDER BY ts DESC LIMIT 1").fetchone()
        if last and last[1] == "arrived":
            # A restart mid-session should not invent a departure.
            self.here, self.since = True, last[0]

    def close(self):
        self.db.close()

    def sample(self, present, conf=None, view_ok=True, at=None):
        """Record one look. Returns "arrived", "left" or None.

        `view_ok` False with no detection is stored but cannot advance a
        departure -- it is a look at the wrong place, not evidence of absence.
        """
        at = self.clock() if at is None else float(at)
        self.db.execute("INSERT INTO samples VALUES (?,?,?,?)",
                        (at, int(bool(present)), conf, int(bool(view_ok))))
        event = None
        if present:
            self._seen_run += 1
            self._gone_run = 0
            if not self.here and self._seen_run >= self.arrive_after:
                self.here, self.since, event = True, at, "arrived"
        elif view_ok:
            self._gone_run += 1
            self._seen_run = 0
            if self.here and self._gone_run >= self.leave_after:
                self.here, self.since, event = False, at, "left"
        if event:
            self.db.execute("INSERT INTO events VALUES (?,?)", (at, event))
        self.db.commit()
        return event

    def log_event(self, kind, at=None):
        """Record something AVI did about his presence, e.g. a break offer."""
        at = self.clock() if at is None else float(at)
        self.db.execute("INSERT INTO events VALUES (?,?)", (at, str(kind)))
        self.db.commit()

    def recent(self, n=20):
        """The last n raw looks, newest first. For checking, not summarising:
        an hourly fraction hid a false arrival that the raw looks show."""
        return [{"ts": round(ts, 1), "present": bool(pr),
                 "conf": None if c is None else round(c, 3),
                 "view_ok": bool(v)}
                for ts, pr, c, v in self.db.execute(
                    "SELECT ts, present, conf, view_ok FROM samples "
                    "ORDER BY ts DESC LIMIT ?", (int(n),))]

    # --- what has been learned ------------------------------------------
    def sessions(self, since=None, min_minutes=MIN_SESSION_MIN):
        """[(start, end_or_None), ...] from the arrival/departure events."""
        q = "SELECT ts, kind FROM events"
        args = ()
        if since is not None:
            q += " WHERE ts >= ?"
            args = (float(since),)
        out, start = [], None
        for ts, kind in self.db.execute(q + " ORDER BY ts", args):
            if kind == "arrived" and start is None:
                start = ts
            elif kind == "left" and start is not None:
                if (ts - start) / 60.0 >= min_minutes:
                    out.append((start, ts))
                start = None
        if start is not None:
            out.append((start, None))
        return out

    def break_outcomes(self, within_s=BREAK_TAKEN_WITHIN_S, now=None):
        """[(offered_ts, "taken" | "ignored"), ...], oldest first.

        Taken: he left within `within_s`. Ignored: he was still seen at the
        desk after that. Neither -- the offer is too recent, or nothing was
        watching afterwards (the dashboard stopped) -- is not judged: a run
        that ended is not him saying no.
        """
        now = self.clock() if now is None else now
        offers = [r[0] for r in self.db.execute(
            "SELECT ts FROM events WHERE kind = 'break_offered' ORDER BY ts")]
        out = []
        for ts in offers:
            if now - ts < within_s:
                continue
            left = self.db.execute(
                "SELECT 1 FROM events WHERE kind = 'left' AND ts > ? "
                "AND ts <= ? LIMIT 1", (ts, ts + within_s)).fetchone()
            if left:
                out.append((ts, "taken"))
                continue
            stayed = self.db.execute(
                "SELECT 1 FROM samples WHERE present = 1 AND ts > ? "
                "AND ts <= ? LIMIT 1",
                (ts + within_s, ts + within_s + 600.0)).fetchone()
            if stayed:
                out.append((ts, "ignored"))
        return out

    def routine(self, min_samples=10):
        """Presence by hour of day, from VALID looks only.

        Each hour reports the fraction of valid looks that found him and how
        many looks that is, so a pattern resting on three samples is visible
        as exactly that rather than as a confident 100%.
        """
        rows = self.db.execute(
            "SELECT ts, present FROM samples WHERE view_ok = 1 OR present = 1"
        ).fetchall()
        hours = {}
        for ts, present in rows:
            h = datetime.fromtimestamp(ts).hour
            n, k = hours.get(h, (0, 0))
            hours[h] = (n + 1, k + int(present))
        out = {}
        for h, (n, k) in sorted(hours.items()):
            out[h] = {"looks": n, "present": round(k / n, 3),
                      "enough": n >= min_samples}
        return out

    def summary(self, now=None):
        now = self.clock() if now is None else now
        day0 = datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = self.sessions(since=day0, min_minutes=0)
        minutes = sum(((e or now) - s) / 60.0 for s, e in today)
        total, valid = self.db.execute(
            "SELECT COUNT(*), SUM(view_ok) FROM samples WHERE ts >= ?",
            (day0,)).fetchone()
        out = {"here": self.here,
               "for_minutes": (round((now - self.since) / 60.0, 1)
                               if self.here and self.since else None),
               "today_minutes": round(minutes, 1),
               "sessions_today": len(today),
               "looks_today": int(total or 0),
               # How much of the day could actually have seen him. Without
               # this a low "today_minutes" is ambiguous: away, or unwatched.
               "coverage_today": (round((valid or 0) / total, 3)
                                  if total else None),
               "breaks_offered_today": self.db.execute(
                   "SELECT COUNT(*) FROM events WHERE kind = 'break_offered'"
                   " AND ts >= ?", (day0,)).fetchone()[0]}
        judged = self.break_outcomes(now=now)[-10:]
        if judged:
            out["breaks_judged"] = len(judged)
            out["breaks_taken"] = sum(1 for _, o in judged if o == "taken")
        # The first sit-down of each day, not every return from a break
        # (perception/habits.py). Hours past midnight run on (24.5 = 00:30)
        # so a late evening averages with the evening, not the morning.
        h = habits.summarize(self.sessions(min_minutes=MIN_SESSION_MIN))
        if h:
            out["usual_arrival_hour"] = h["usual_start"]
            out["arrivals_seen"] = h["days_seen"]
            for key, name in (("usual_end", "usual_leave_hour"),
                              ("usual_session_min", "usual_session_min"),
                              ("usual_break_min", "usual_break_min")):
                if key in h:
                    out[name] = h[key]
        return out
