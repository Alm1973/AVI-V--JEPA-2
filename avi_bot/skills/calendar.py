"""The calendar, read-only, through its secret iCal address. BUILD_SPEC 9.

NO LOGIN, NO OAUTH, ON PURPOSE. Google's OAuth tokens expire after 7 days
while an app is in "Testing", which would mean re-authorising a desk robot
every week forever. The secret iCal URL has no such expiry.

THE URL IS THE CREDENTIAL. Anyone holding it can read his calendar
indefinitely, so it lives in .env, never in git, and never in a prompt --
`providers.redact()` strips it on the way to any model, and this module
deliberately returns EVENTS rather than the URL so nothing downstream has to
be trusted with it.

Two things the spec asks for and the fixture test covers: the right events for
a day, and the right reminder times (10 minutes before). All-day events are
handled separately because a deadline with no clock time should not produce a
"in 10 minutes" nudge at midnight.
"""
import datetime as dt

REMIND_BEFORE_S = 600          # 10 minutes, BUILD_SPEC 9
RUNDOWN_ITEMS = 3


class Event:
    __slots__ = ("summary", "start", "end", "all_day", "location", "uid")

    def __init__(self, summary, start, end=None, all_day=False,
                 location=None, uid=None):
        self.summary = summary
        self.start = start
        self.end = end
        self.all_day = all_day
        self.location = location
        self.uid = uid

    def __repr__(self):
        when = "all day" if self.all_day else self.start.strftime("%H:%M")
        return f"<Event {when} {self.summary}>"

    def spoken_time(self):
        """'10', 'half past 6', 'all day' -- short, for the 20-word cap."""
        if self.all_day:
            return "all day"
        minute = self.start.minute
        hour = self.start.hour % 12 or 12
        if minute == 0:
            return str(hour)
        if minute == 30:
            return f"half past {hour}"
        return f"{hour}:{minute:02d}"

    def remind_at(self):
        """When to speak about it, or None for an all-day event."""
        if self.all_day:
            return None
        return self.start - dt.timedelta(seconds=REMIND_BEFORE_S)


def _as_local(value, tz):
    """iCal gives dates for all-day events and datetimes otherwise."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz), False
        return value.astimezone(tz), False
    # A bare date: all-day. Midnight local, flagged.
    return dt.datetime(value.year, value.month, value.day, tzinfo=tz), True


def parse(ics_text, start, end, tz=None):
    """Events between `start` and `end`, recurrences expanded.

    Uses icalendar plus recurring-ical-events, which is what BUILD_SPEC 12
    specifies -- rolling our own RRULE expansion would be the single most
    error-prone thing in this repo for no benefit.
    """
    import icalendar
    import recurring_ical_events
    tz = tz or start.tzinfo or dt.timezone.utc
    cal = icalendar.Calendar.from_ical(ics_text)
    found = recurring_ical_events.of(cal).between(start, end)
    out = []
    for raw in found:
        begin, all_day = _as_local(raw.get("DTSTART").dt, tz)
        finish = None
        if raw.get("DTEND") is not None:
            finish, _ = _as_local(raw.get("DTEND").dt, tz)
        out.append(Event(str(raw.get("SUMMARY") or "(untitled)"),
                         begin, finish, all_day=all_day,
                         location=str(raw.get("LOCATION"))
                         if raw.get("LOCATION") else None,
                         uid=str(raw.get("UID") or "")))
    out.sort(key=lambda e: (e.start, e.all_day))
    return out


def day_bounds(day, now, tz):
    """(start, end) for 'today' / 'tomorrow' / 'this week'."""
    today = now.astimezone(tz).replace(hour=0, minute=0, second=0,
                                       microsecond=0)
    if day == "tomorrow":
        start = today + dt.timedelta(days=1)
        return start, start + dt.timedelta(days=1)
    if day == "this week":
        return today, today + dt.timedelta(days=7)
    return today, today + dt.timedelta(days=1)


class Calendar:
    def __init__(self, ics_text=None, url=None, tz=None, clock=None,
                 fetch=None):
        self.ics_text = ics_text
        self.url = url
        self.tz = tz or dt.timezone(dt.timedelta(hours=-7))   # Phoenix, no DST
        self._now = clock or (lambda: dt.datetime.now(self.tz))
        self.fetch = fetch
        self._reminded = set()
        self._rundown_date = None

    def clock(self):
        """Now, as an aware datetime -- whatever the injected clock returns.

        A float epoch is accepted and converted. This matters more than it
        looks: every other clock in AVI (store, timers, focus, modes) returns
        epoch seconds, this one wanted a datetime, and the mismatch is why
        main.py constructed the calendar with no clock at all. The calendar
        then read real time while the rest of Avi read the injected clock, so
        one object ran on two timelines -- which left a test failing for one
        minute a day, and no calendar behaviour reachable through Avi pinnable
        to a time at all.
        """
        now = self._now()
        if isinstance(now, (int, float)):
            return dt.datetime.fromtimestamp(now, tz=self.tz)
        return now

    def _text(self):
        if self.ics_text is not None:
            return self.ics_text
        if self.fetch is not None:
            return self.fetch(self.url)
        if not self.url:
            return None
        import urllib.request
        with urllib.request.urlopen(self.url, timeout=10) as r:
            return r.read().decode("utf-8", "replace")

    def events(self, day="today"):
        """Events for a day, or [] if the feed is unreachable.

        An unreachable calendar is a silent empty day rather than an
        exception: this is polled every ten minutes from a loop that also
        drives the arm.
        """
        text = self._text()
        if not text:
            return []
        start, end = day_bounds(day, self.clock(), self.tz)
        try:
            return parse(text, start, end, tz=self.tz)
        except Exception:
            return []

    def describe(self, day="today"):
        """One sentence inside the speech cap."""
        found = self.events(day)
        when = "today" if day == "today" else day
        if not found:
            return f"Nothing {when}."
        head = found[:RUNDOWN_ITEMS]
        bits = [f"{e.summary} at {e.spoken_time()}" if not e.all_day
                else e.summary for e in head]
        more = len(found) - len(head)
        text = "; ".join(bits)
        return f"{text}." if not more else f"{text}; and {more} more."

    def rundown(self, now=None):
        """The morning rundown, at most once a day. BUILD_SPEC 9.

        Returns None if it has already been given today, so the caller can
        offer it on every sit-down without repeating itself.
        """
        now = now or self.clock()
        today = now.astimezone(self.tz).date()
        if self._rundown_date == today:
            return None
        self._rundown_date = today
        found = self.events("today")
        if not found:
            return "Nothing on today."
        bits = [f"{e.summary} at {e.spoken_time()}" if not e.all_day
                else e.summary for e in found[:RUNDOWN_ITEMS]]
        return f"{len(found)} today: " + ", ".join(bits) + "."

    def due_reminders(self, now=None):
        """Events whose 10-minute warning has arrived and not yet been given.

        Each event reminds ONCE. The caller polls this every few seconds and a
        reminder repeated every poll is the fastest way to be switched off.
        """
        now = now or self.clock()
        out = []
        for event in self.events("today") + self.events("tomorrow"):
            at = event.remind_at()
            if at is None:
                continue
            key = (event.uid, event.start.isoformat())
            if key in self._reminded:
                continue
            if at <= now < event.start:
                self._reminded.add(key)
                out.append(event)
        return out
