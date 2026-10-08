"""Facts he told AVI that name a day: "chemistry test on Friday".

The store kept the sentence and nothing looked at the day in it. On
Thursday 2026-10-08 he sat down, the morning rundown found no calendar and
no reminders, and said nothing -- with a test the next day sitting in
memory. This reads the day out of a fact, RELATIVE TO WHEN HE SAID IT ("on
Friday" told on a Monday is that week's Friday, not today's next one), so
the rundown can say "Tomorrow: chemistry test."

Deliberately narrow: weekday names, "tomorrow", and month-day dates. A fact
with no day, or one this cannot read, is simply not dated -- a wrong date
said confidently is worse than none.
"""
import re
from datetime import date, datetime, timedelta

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december")

_WEEKDAY = re.compile(r"\b(next\s+)?(" + "|".join(WEEKDAYS) + r")(s)?\b",
                      re.I)
_TOMORROW = re.compile(r"\btomorrow\b", re.I)
_MONTH_DAY = re.compile(
    r"\b(" + "|".join(m[:3] for m in MONTHS) + r")[a-z]*\.?\s+(\d{1,2})"
    r"(?:st|nd|rd|th)?\b", re.I)
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" +
    "|".join(m[:3] for m in MONTHS) + r")[a-z]*\b", re.I)
# A birthday or anniversary is a date every year, not an event ahead.
_YEARLY = re.compile(r"\b(birthday|anniversary|born)\b", re.I)


def day_named(text, said_ts, today=None):
    """The date a fact refers to, read relative to when it was said, or
    None. "on Friday" said on a Friday means the NEXT Friday: nobody tells
    a robot about something happening today as "on Friday". "on Mondays"
    is every week: its next one from `today` (default: the day said)."""
    said = datetime.fromtimestamp(float(said_ts)).date()
    t = text or ""
    if _YEARLY.search(t):
        return None
    if _TOMORROW.search(t):
        return said + timedelta(days=1)
    m = _WEEKDAY.search(t)
    if m:
        want = WEEKDAYS.index(m.group(2).lower())
        if m.group(3):                       # "on Mondays": every week
            base = today or said
            return base + timedelta(days=(want - base.weekday()) % 7)
        if m.group(1) and said.weekday() < want:
            # "next Friday" said on a Monday: this week's or the week
            # after's? People mean both. Not dated rather than guessed.
            return None
        ahead = (want - said.weekday()) % 7 or 7
        return said + timedelta(days=ahead)
    m = _MONTH_DAY.search(t)
    if m:
        mon = [x[:3] for x in MONTHS].index(m.group(1).lower()[:3]) + 1
        return _next_date(said, mon, int(m.group(2)))
    m = _DAY_MONTH.search(t)
    if m:
        mon = [x[:3] for x in MONTHS].index(m.group(2).lower()[:3]) + 1
        return _next_date(said, mon, int(m.group(1)))
    return None


def _next_date(said, month, day):
    """That month-day on or after the day it was said."""
    for year in (said.year, said.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= said:
            return d
    return None


def what(text):
    """The fact without its day words, for "Tomorrow: chemistry test."""
    t = _WEEKDAY.sub("", text or "")
    t = _TOMORROW.sub("", t)
    t = _MONTH_DAY.sub("", t)
    t = _DAY_MONTH.sub("", t)
    t = re.sub(r"\b(on|this|is|at)\s*$", "", t.strip(), flags=re.I)
    t = re.sub(r"\s+(on|this)\s*(?=[,.]|$)", "", t, flags=re.I)
    t = " ".join(t.split()).strip(" ,.;:-")
    return t


def ahead(facts, today):
    """[(date, words)] for facts that fall today or tomorrow.

    facts: [{"text", "created_at"}]. Ordered by date, then as stored.
    """
    out = []
    for f in facts:
        d = day_named(f["text"], f["created_at"], today=today)
        if d is None or not today <= d <= today + timedelta(days=1):
            continue
        words = what(f["text"])
        if words:
            out.append((d, words))
    out.sort(key=lambda x: x[0])
    return out
