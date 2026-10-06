"""Timers that survive a restart. BUILD_SPEC 9.

The acceptance test is "a 5 s timer fires, and still fires after a restart",
and the second half is the whole design. A timer held in memory is lost when
the dashboard restarts, which happens often -- it restarted a dozen times in
one evening while look-at was being fixed. A timer that quietly disappears is
worse than no timer at all, because he stopped watching the clock.

So the due time is written to SQLite immediately and the in-process scheduler
is only a convenience. `due()` re-reads the table, which means a timer set
before a crash fires after it, late rather than never.
"""
import time

from ..memory import db as memdb

KIND = "timer"


class Timers:
    def __init__(self, store=None, clock=time.time):
        self.store = store or memdb.Store()
        self.clock = clock

    def set(self, seconds, label=None):
        """Start a timer. Returns its row id."""
        seconds = int(seconds)
        if seconds < 1:
            raise ValueError("a timer needs at least one second")
        due = self.clock() + seconds
        text = label or f"{self._spoken(seconds)} timer"
        return self.store.add_reminder(due, text, kind=KIND)

    def cancel(self, tid=None):
        """Cancel one timer, or all of them. Returns how many were cancelled."""
        rows = self.pending()
        if tid is not None:
            rows = [r for r in rows if r["id"] == tid]
        for row in rows:
            self.store.mark_reminder(row["id"], status="cancelled")
        return len(rows)

    def pending(self):
        rows = self.store.db.execute(
            "SELECT * FROM reminders WHERE kind=? AND status='pending'"
            " ORDER BY due_ts", (KIND,)).fetchall()
        return [dict(r) for r in rows]

    def due(self, now=None):
        """Timers whose time has come, read from the TABLE not from memory.

        Does not mark them done -- the caller marks each one after it has
        actually chirped, so a crash in between loses a timer loudly rather
        than eating it silently.
        """
        now = self.clock() if now is None else now
        return [r for r in self.store.due_reminders(now) if r["kind"] == KIND]

    def fired(self, tid):
        self.store.mark_reminder(tid, status="done")

    def remaining(self, tid):
        row = self.store.db.execute(
            "SELECT due_ts FROM reminders WHERE id=?", (tid,)).fetchone()
        if row is None:
            return None
        return max(0.0, row["due_ts"] - self.clock())

    # --- speaking about them -------------------------------------------
    @staticmethod
    def _spoken(seconds):
        """'20 minute', '1 hour', '30 second' -- for a label, so the chirp can
        be followed by something that sounds like English."""
        if seconds % 3600 == 0 and seconds >= 3600:
            n = seconds // 3600
            return f"{n} hour" if n == 1 else f"{n} hour"
        if seconds % 60 == 0 and seconds >= 60:
            return f"{seconds // 60} minute"
        return f"{seconds} second"

    def describe(self):
        """One short sentence, within the 20-word speech cap."""
        rows = self.pending()
        if not rows:
            return "No timers."
        if len(rows) == 1:
            left = max(0, int(rows[0]["due_ts"] - self.clock()))
            return f"{self._spoken(left)}s left on {rows[0]['text']}."
        return f"{len(rows)} timers running."
