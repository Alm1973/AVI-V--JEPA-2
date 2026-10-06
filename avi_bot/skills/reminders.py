"""Natural times, parsed locally. BUILD_SPEC 9's test: 10 phrasings parse.

BUILD_SPEC allows "the LLM or dateparser". This does neither by default, for
two reasons worth stating:

  The LLM is the wrong first choice because "remind me in 20 minutes" is one
  of the frequent commands, and a reminder that needs a working free tier to
  be SET is not a reminder. The local router already handles the common
  phrasings instantly and offline; parsing has to work on the same terms.

  dateparser is a large dependency that solves a much bigger problem than
  this -- every locale, every calendar. The phrasings a person actually uses
  with a desk robot are a short list, and a short list is testable.

So: a focused parser that RETURNS NONE rather than guessing. A wrong reminder
time is worse than a refused one, because he stops checking. When this returns
None the caller hands the phrase to the LLM, which is where an unusual
phrasing belongs.

AMBIGUOUS CLOCK TIMES RESOLVE FORWARD. "at 5" said at 9am means 5pm today;
said at 6pm it means 5am tomorrow. Nobody sets a reminder for the past.
"""
import datetime as dt
import re

UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
}

# Named times of day, as (hour, minute).
NAMED = {
    "noon": (12, 0), "midday": (12, 0), "midnight": (0, 0),
    "morning": (8, 0), "afternoon": (14, 0), "evening": (19, 0),
    "tonight": (21, 0), "dinner": (18, 30), "lunch": (12, 30),
    "breakfast": (7, 30), "bedtime": (22, 0),
}

# Units LONGEST FIRST. Regex alternation is ordered, and with "s" early the
# phrase "in two hours" matched n="two hour" and unit="s" -- a valid overall
# match whose number then failed to parse, so the whole phrase was refused.
# Sorting by length makes the longest unit win before a one-letter one can
# claim a trailing character.
_UNIT_RE = "|".join(sorted((re.escape(u) for u in UNITS), key=len,
                           reverse=True))
# The NUMBER is a closed vocabulary too, so enumerate it rather than letting
# [a-z]+ chew into the unit. With a generic word pattern, "in two hours"
# matched n="two hour" and unit="s" -- a perfectly valid overall match whose
# number then failed to parse, and parse() bails on the first match rather
# than hunting for a second reading. Listing the words removes the ambiguity
# instead of relying on backtracking to resolve it.
_NUM_RE = "|".join(sorted((re.escape(w) for w in WORDS), key=len,
                          reverse=True))
_IN = re.compile(
    rf"^in\s+(?P<n>\d+|{_NUM_RE})\s*"
    rf"(?P<unit>{_UNIT_RE})$", re.I)
# Dots in am/pm are optional on BOTH sides: the phrase is stripped of trailing
# punctuation before it gets here, so "7:45 p.m." arrives as "7:45 p.m".
_AT = re.compile(
    r"^(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*"
    r"(?P<ampm>[ap]\.?m\.?)?$", re.I)
_TOMORROW_AT = re.compile(
    r"^tomorrow(?:\s+at)?\s+(?P<rest>.+)$", re.I)
_NAMED_AT = re.compile(r"^(?:at\s+|this\s+|in\s+the\s+)?(?P<name>"
                       + "|".join(NAMED) + r")$", re.I)
_HALF = re.compile(r"^in\s+half\s+an?\s+hour$", re.I)


def _number(text):
    t = " ".join(text.split()).lower()
    if t.isdigit():
        return int(t)
    return WORDS.get(t)


def parse(phrase, now):
    """A phrase plus the current time -> a datetime, or None.

    `now` is passed in rather than read, so every case is testable at a fixed
    instant and the forward-resolution rule can be checked from both sides of
    the clock.
    """
    if not phrase:
        return None
    text = " ".join(str(phrase).split()).strip().lower().rstrip(".!?")
    if not text:
        return None

    if _HALF.match(text):
        return now + dt.timedelta(minutes=30)

    m = _IN.match(text)
    if m:
        n = _number(m.group("n"))
        unit = UNITS.get(m.group("unit").lower())
        if n is None or unit is None or n <= 0:
            return None
        return now + dt.timedelta(seconds=n * unit)

    m = _NAMED_AT.match(text)
    if m:
        hour, minute = NAMED[m.group("name").lower()]
        return _forward(now, hour, minute)

    m = _TOMORROW_AT.match(text)
    if m:
        rest = m.group("rest").strip()
        named = _NAMED_AT.match(rest)
        if named:
            hour, minute = NAMED[named.group("name").lower()]
        else:
            clock = _AT.match(rest)
            if not clock:
                return None
            got = _clock_parts(clock)
            if got is None:
                return None
            hour, minute = got
        base = (now + dt.timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        return base

    m = _AT.match(text)
    if m:
        got = _clock_parts(m)
        if got is None:
            return None
        hour, minute = got
        explicit = bool(m.group("ampm"))
        return _forward(now, hour, minute, assume_pm=not explicit)

    return None


def _clock_parts(match):
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    ampm = (match.group("ampm") or "").replace(".", "").strip().lower()
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    elif hour > 23:
        return None
    return hour, minute


def _forward(now, hour, minute, assume_pm=False):
    """The EARLIEST future time a clock would read this. Never the past.

    Stated that way the rule has no special cases. With no am/pm a bare 1-11
    could be either half of the day, so both are candidates on today and on
    tomorrow, and the soonest one that has not already passed wins:

        "at 5" said at 09:00  ->  17:00 today    (today's 05:00 has gone)
        "at 5" said at 18:00  ->  05:00 tomorrow (both of today's have gone)

    An earlier version ordered the candidates and fell back to the first one,
    which gave 17:00 TOMORROW for the second case -- a whole half-day late,
    from code that looked like it was choosing carefully.
    """
    base = now.replace(second=0, microsecond=0)
    hours = [hour]
    if assume_pm and 1 <= hour <= 11:
        hours.append(hour + 12)
    candidates = []
    for day in (0, 1):
        for h in hours:
            candidates.append(
                base.replace(hour=h % 24, minute=minute)
                + dt.timedelta(days=day))
    future = sorted(c for c in candidates if c > now)
    return future[0] if future else None


def make(store, clock=None):
    """Handler for Registry.register. Returns a refusal if the time is unclear."""
    clock = clock or (lambda: dt.datetime.now())

    def set_reminder(when, text):
        now = clock()
        at = parse(when, now)
        if at is None:
            # Deliberately a refusal, not a guess. The caller can hand the
            # phrase to the LLM, which is where an unusual wording belongs.
            return f"When is {when!r}?", {"parsed": None}
        store.add_reminder(at.timestamp(), text)
        delta = at - now
        if delta.total_seconds() < 3600:
            return (f"In {int(delta.total_seconds() // 60)} minutes.",
                    {"parsed": at.isoformat()})
        return at.strftime("Set for %-I:%M %p.").replace(" 0", " "), \
            {"parsed": at.isoformat()}

    return {"set_reminder": set_reminder}
