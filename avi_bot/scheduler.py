"""Things that come due on their own: timers, reminders, calendar warnings.

Everything else in AVI is reactive -- he says something, it answers. This is
the one part that speaks without being spoken to, which makes it the part most
able to be annoying, so it has three rules:

  ONE AT A TIME. If a timer and a calendar warning come due in the same tick,
  one is spoken and the other waits. Two announcements at once is noise, and
  the second is the one that gets ignored.

  SPOKEN, THEN MARKED. A reminder is marked done only after the caller
  confirms it was delivered, so a crash between the two loses it loudly
  instead of eating it silently.

  SILENT MODES ARE HONOURED, BUT TIME STILL PASSES. During quiet or guest
  mode nothing is announced, and when it ends the still-relevant items are
  delivered while the stale ones are dropped -- a timer from forty minutes ago
  is not news.
"""
import time

TIMER = "timer"
REMINDER = "reminder"
EVENT = "event"

# Past this, an announcement is history rather than news and is dropped with a
# note. Sized so a short quiet period delivers everything and a long one does
# not produce a pile-up.
STALE_AFTER_S = 600.0

# Except a reminder. A timer forty minutes late is not news; "call mom at 3"
# at 3:40 still is, because the thing itself has not been done. A stale
# reminder is marked MISSED instead of done, and whoever delivers (the
# dashboard's brain host) tells him when he is back -- "while you were away".
MISSED = "missed"


class Announcement:
    __slots__ = ("kind", "text", "beep", "due_at", "ref", "stale")

    def __init__(self, kind, text, beep=None, due_at=None, ref=None,
                 stale=False):
        self.kind = kind
        self.text = text
        self.beep = beep
        self.due_at = due_at
        self.ref = ref
        self.stale = stale

    def __repr__(self):
        return (f"<Announcement {self.kind} {self.text!r}"
                f"{' STALE' if self.stale else ''}>")


class Scheduler:
    def __init__(self, timers=None, store=None, calendar=None, modes=None,
                 clock=time.time, stale_after_s=STALE_AFTER_S):
        self.timers = timers
        self.store = store
        self.calendar = calendar
        self.modes = modes
        self.clock = clock
        self.stale_after_s = stale_after_s
        self.dropped = 0           # stale announcements never made
        self.missed = 0            # stale reminders kept to tell him later
        self.delivered = 0
        # Calendar warnings have to be BUFFERED, unlike timers and reminders.
        # Timers live in a table and stay due until marked, so polling them is
        # free. Calendar.due_reminders() deduplicates ON READ -- each event
        # warns once -- so a read that does not end in delivery LOSES it.
        # due() is polled by design, so without this buffer any calendar
        # warning that was not the soonest item vanished silently. Found by
        # the speech-cap test, which expected three announcements and got two.
        self._held = []

    def _muted(self):
        if self.modes is None:
            return False
        return self.modes.is_quiet or self.modes.asleep or self.modes.guest

    def muted(self):
        """Quiet, asleep or guest mode: nothing should be said unasked."""
        return self._muted()

    def due(self, now=None):
        """Everything waiting, soonest first. Does not consume or mark."""
        now = self.clock() if now is None else now
        out = []
        if self.timers is not None:
            for row in self.timers.due(now):
                out.append(Announcement(
                    TIMER, f"Timer: {row['text']}.", beep="alert",
                    due_at=row["due_ts"], ref=row["id"]))
        if self.store is not None:
            for row in self.store.due_reminders(now):
                if row.get("kind") == "timer":
                    continue              # already handled above
                out.append(Announcement(
                    REMINDER, row["text"], beep="alert",
                    due_at=row["due_ts"], ref=row["id"]))
        if self.calendar is not None:
            try:
                for event in self.calendar.due_reminders():
                    self._held.append(Announcement(
                        EVENT, f"{event.summary} in ten minutes.",
                        beep="alert", due_at=now, ref=event.uid))
            except Exception:
                pass                      # a calendar outage is not an alarm
        out.extend(self._held)
        for a in out:
            if a.due_at is not None and now - a.due_at > self.stale_after_s:
                a.stale = True
        out.sort(key=lambda a: (a.due_at if a.due_at is not None else now))
        return out

    def next_announcement(self, now=None):
        """The single thing to say, or None.

        Stale items are retired here rather than returned, so the caller never
        has to decide whether something is still worth saying.
        """
        now = self.clock() if now is None else now
        waiting = self.due(now)
        for a in waiting:
            if a.stale:
                if a.kind == REMINDER and self.store is not None:
                    self.hold(a)
                else:
                    self.retire(a)
                    self.dropped += 1
                continue
            if self._muted():
                return None
            return a
        return None

    def delivered_ok(self, announcement):
        """Called after it was actually spoken or chirped."""
        self.retire(announcement)
        self.delivered += 1

    def retire(self, announcement):
        """Mark it done so it does not come back. Separate from delivery on
        purpose: a stale item is retired WITHOUT being counted as delivered."""
        if announcement.kind == TIMER and self.timers is not None:
            self.timers.fired(announcement.ref)
        elif announcement.kind == REMINDER and self.store is not None:
            self.store.mark_reminder(announcement.ref)
        elif announcement.kind == EVENT:
            self._held = [a for a in self._held if a is not announcement]

    def hold(self, announcement):
        """A reminder that could not be told now (stale, or nobody there):
        kept as MISSED so it is told later instead of lost."""
        if announcement.kind != REMINDER or self.store is None:
            return False
        self.store.mark_reminder(announcement.ref, MISSED)
        self.missed += 1
        return True

    def missed_reminders(self):
        """[{"id", "text", "due_ts", ...}] still owed, oldest first."""
        if self.store is None:
            return []
        return self.store.reminders(MISSED)

    def told_missed(self, rid):
        if self.store is not None:
            self.store.mark_reminder(rid, "done")
        self.delivered += 1

    def pending_count(self, now=None):
        return len(self.due(now))
