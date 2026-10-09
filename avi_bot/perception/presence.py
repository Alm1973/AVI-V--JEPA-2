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
  was looking at his seat; only those can count as "away". (A detection used
  to count from anywhere -- "if he is seen, he is there" -- until a poster on
  his wall, seen mid-dance from a new angle, "arrived" 40 s after he left,
  2026-10-09. The dashboard now samples only the seat view, held still.)

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

from avi_bot.memory import locked
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
-- "People" in the seat view that are not him: a box that sat perfectly
-- still for five minutes (a poster, a photo). Learned, not configured.
CREATE TABLE IF NOT EXISTS decoys (
    id         INTEGER PRIMARY KEY,
    box        TEXT NOT NULL,      -- cx,cy,w,h as fractions of the frame
    learned_at REAL NOT NULL,
    seen_at    REAL,
    hits       INTEGER NOT NULL DEFAULT 0,
    moved      INTEGER NOT NULL DEFAULT 0
);
"""


def iou(a, b):
    """Overlap of two (cx, cy, w, h) boxes, 0..1."""
    ax0, ay0 = a[0] - a[2] / 2, a[1] - a[3] / 2
    bx0, by0 = b[0] - b[2] / 2, b[1] - b[3] / 2
    ix = max(0.0, min(ax0 + a[2], bx0 + b[2]) - max(ax0, bx0))
    iy = max(0.0, min(ay0 + a[3], by0 + b[3]) - max(ay0, by0))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def seat_return_due(now, hold_until, busy=False, surveying=False,
                    paused=False, tracking=False, at_seat=False):
    """Should an idle arm go back to watching his seat?

    Absence only counts from the seat view, so an arm left pointing
    anywhere else -- after a restart, a slider session, a find -- means he
    can never be seen to LEAVE: "here" sticks, a reminder is "told" to an
    empty room, and sessions never end. Nothing else may want the arm.
    """
    if at_seat or busy or surveying or paused or tracking:
        return False
    return now >= hold_until


class Presence:
    def __init__(self, path=":memory:", clock=time.time,
                 arrive_after=ARRIVE_AFTER, leave_after=LEAVE_AFTER):
        self.db = locked.connect(path)
        self.db.executescript(SCHEMA)
        # Where the person box was, and the light, per sample (numbers only).
        # 2026-10-08: a "person" at 0.89 in every sample from 08:28 to 18:19
        # while he was at school -- someone else, or a picture of someone?
        # The box over time tells a still picture from a living person.
        have = {r[1] for r in self.db.execute("PRAGMA table_info(samples)")}
        for col in ("box", "light"):
            if col not in have:
                self.db.execute(f"ALTER TABLE samples ADD COLUMN {col} "
                                + ("TEXT" if col == "box" else "REAL"))
        self.db.commit()
        self.clock = clock
        self.arrive_after = int(arrive_after)
        self.leave_after = int(leave_after)
        self.here = False
        self._seen_run = 0
        self._gone_run = 0
        self.since = None
        # The last arrival or departure: a break offer is logged here too,
        # and reading IT as the last event made every restart after one an
        # "arrival" -- with a second morning rundown (2026-10-09 07:21).
        last = self.db.execute(
            "SELECT ts, kind FROM events WHERE kind IN ('arrived', 'left')"
            " ORDER BY ts DESC LIMIT 1").fetchone()
        if last and last[1] == "arrived":
            # A restart mid-session should not invent a departure.
            self.here, self.since = True, last[0]

    def close(self):
        self.db.close()

    def sample(self, present, conf=None, view_ok=True, at=None, box=None,
               light=None):
        """Record one look. Returns "arrived", "left" or None.

        `view_ok` False with no detection is stored but cannot advance a
        departure -- it is a look at the wrong place, not evidence of absence.
        `box` is the best person box as fractions of the frame (cx, cy, w,
        h); `light` the frame's mean level.
        """
        at = self.clock() if at is None else float(at)
        self.db.execute(
            "INSERT INTO samples (ts, present, conf, view_ok, box, light) "
            "VALUES (?,?,?,?,?,?)",
            (at, int(bool(present)), conf, int(bool(view_ok)),
             None if box is None else ",".join(f"{v:.4f}" for v in box),
             None if light is None else round(float(light), 1)))
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
    def still_person(self, n=15, tol=0.006, size_tol=0.012):
        """Has the 'person' in the last `n` present samples (5 minutes at
        one per 20 s) not moved at all? A living person shifts more than
        this while sitting still; a photo or poster does not. None without
        enough boxed samples. Seat-view samples only: box positions from
        two different views are not comparable."""
        rows = self.db.execute(
            "SELECT box FROM samples WHERE present = 1 AND box IS NOT NULL "
            "AND view_ok = 1 ORDER BY ts DESC LIMIT ?", (int(n),)).fetchall()
        if len(rows) < n:
            return None
        boxes = [[float(v) for v in r[0].split(",")] for r in rows]
        span = [max(b[i] for b in boxes) - min(b[i] for b in boxes)
                for i in range(4)]
        return (span[0] <= tol and span[1] <= tol and span[2] <= size_tol
                and span[3] <= size_tol)

    # --- decoys: what looks like a person and is not him ----------------
    # 2026-10-08 08:28-18:19 and 2026-10-09 from 06:12 (sunrise): a 'person'
    # at 0.89 that never moved. It "arrived" at dawn, got the morning
    # rundown, and was offered a break after 59 minutes. Once a box has
    # been perfectly still for five minutes it is remembered as a decoy and
    # a detection on it no longer counts as him.
    DECOY_IOU = 0.5
    # A detection on a decoy that has moved this far (a fraction of the
    # frame, centre or size) is someone alive there, not the picture.
    DECOY_MOVED = 0.02
    # Three such and the "decoy" was a person sitting very still: dropped.
    DECOY_FORGET_AFTER = 3

    # How far sideways a decoy may appear and still be the decoy: base_yaw
    # backlash (~13 degrees, ~0.15 of the frame) plus margin.
    DECOY_SLIDE = 0.25
    # ...and up or down: the pitch joints' slack, ~1.5 degrees (0.05 of the
    # frame's height) before every joint arrived from one side.
    DECOY_SLIDE_Y = 0.05

    def _same_shape(self, box, d):
        return (abs(box[0] - d[0]) <= self.DECOY_SLIDE
                and abs(box[1] - d[1]) <= self.DECOY_SLIDE_Y
                and abs(box[2] - d[2]) <= self.DECOY_MOVED
                and abs(box[3] - d[3]) <= self.DECOY_MOVED)

    def decoys(self):
        return [{"id": r[0], "box": tuple(float(v) for v in r[1].split(",")),
                 "hits": r[2], "moved": r[3]}
                for r in self.db.execute(
                    "SELECT id, box, hits, moved FROM decoys")]

    def learn_decoy(self):
        """If the person in view has been perfectly still for five minutes,
        remember its box as a decoy. Returns the box when newly learned."""
        if not self.still_person():
            return None
        r = self.db.execute(
            "SELECT box FROM samples WHERE present = 1 AND box IS NOT NULL "
            "AND view_ok = 1 ORDER BY ts DESC LIMIT 1").fetchone()
        box = tuple(float(v) for v in r[0].split(","))
        if any(iou(box, d["box"]) >= self.DECOY_IOU for d in self.decoys()):
            return None
        self.db.execute("INSERT INTO decoys (box, learned_at) VALUES (?,?)",
                        (",".join(f"{v:.4f}" for v in box), self.clock()))
        self.db.commit()
        return box

    def screen(self, boxes, at=None):
        """Split person boxes seen from the seat view into (real, decoy):
        a box on a learned decoy that has not moved is the decoy."""
        at = self.clock() if at is None else float(at)
        known = self.decoys()
        real, fake = [], []
        for box in boxes:
            same = [k for k in known if self._same_shape(box, k["box"])]
            if same:
                # The decoy, possibly slid sideways: base_yaw's backlash
                # can put the seat view a few degrees off, and a poster
                # does not change its size or height in the picture.
                d = same[0]
                fake.append(box)
                self.db.execute("UPDATE decoys SET hits = hits + 1,"
                                " seen_at = ? WHERE id = ?", (at, d["id"]))
                continue
            d = max(known, key=lambda k: iou(box, k["box"]), default=None)
            if d is None or iou(box, d["box"]) < self.DECOY_IOU:
                real.append(box)
                continue
            if max(abs(a - b) for a, b in zip(box, d["box"])) \
                    > self.DECOY_MOVED:
                real.append(box)
                if d["moved"] + 1 >= self.DECOY_FORGET_AFTER:
                    self.db.execute("DELETE FROM decoys WHERE id = ?",
                                    (d["id"],))
                else:
                    self.db.execute("UPDATE decoys SET moved = moved + 1"
                                    " WHERE id = ?", (d["id"],))
            else:
                fake.append(box)
                self.db.execute("UPDATE decoys SET hits = hits + 1,"
                                " seen_at = ? WHERE id = ?", (at, d["id"]))
        self.db.commit()
        return real, fake

    def arrivals(self, since):
        """How many arrivals were logged since `since`."""
        return self.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'arrived' AND ts >= ?",
            (float(since),)).fetchone()[0]

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
