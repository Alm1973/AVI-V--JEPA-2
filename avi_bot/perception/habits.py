"""What his days look like, from the presence sessions.

presence.summary used to call the median of EVERY arrival his usual start:
coming back from dinner counted the same as sitting down for the day, so the
more breaks he took the later AVI thought he started. A day is the unit here:
when it starts (first arrival), when it ends (last departure), how long he
sits, how long he steps away.

A day runs from DAY_STARTS_HOUR to the same hour next morning, not midnight
to midnight: a session that starts at 00:40 is the end of that evening, and
a midnight cut would make it the first arrival of a "day" that, as far as
his routine goes, has not begun yet.

Nothing is called usual until MIN_DAYS days back it -- one evening is not a
habit, and saying it is would be the confident-and-wrong kind of learning.
"""
from datetime import datetime, timedelta

DAY_STARTS_HOUR = 4
MIN_DAYS = 3
# Longer gaps between two sessions are not a break; he went out, or slept.
BREAK_MAX_MIN = 120.0


def day_of(ts):
    """The date this timestamp's day belongs to (days start at 04:00)."""
    return (datetime.fromtimestamp(ts)
            - timedelta(hours=DAY_STARTS_HOUR)).date()


def day_start_ts(ts):
    """Epoch seconds at which the day containing `ts` began (04:00)."""
    d = day_of(ts)
    return datetime(d.year, d.month, d.day, DAY_STARTS_HOUR).timestamp()


def hour_in_day(ts):
    """Hours since midnight on the day's date: 00:40 the next morning is
    24.67, so the evening and the small hours sort and average together."""
    t = datetime.fromtimestamp(ts)
    h = t.hour + t.minute / 60.0 + t.second / 3600.0
    return h + 24.0 if t.hour < DAY_STARTS_HOUR else h


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def by_day(sessions):
    """{date: [(start, end_or_None), ...]} in time order."""
    out = {}
    for s, e in sorted(sessions):
        out.setdefault(day_of(s), []).append((s, e))
    return out


def summarize(sessions, min_days=MIN_DAYS):
    """The usual shape of a day, or {} when there are too few days.

    Keys (hours are hour_in_day values; minutes are minutes):
      days_seen, usual_start, usual_end, usual_session_min, usual_break_min.
    usual_end and usual_break_min are left out when nothing finished or no
    day had a break; an open session has no end to learn from.
    """
    days = by_day(sessions)
    if len(days) < min_days:
        return {}
    starts = [hour_in_day(ss[0][0]) for ss in days.values()]
    ends = [hour_in_day(ss[-1][1]) for ss in days.values()
            if ss[-1][1] is not None]
    lengths = [(e - s) / 60.0 for ss in days.values() for s, e in ss
               if e is not None]
    gaps = []
    for ss in days.values():
        for (_, e), (s, _) in zip(ss, ss[1:]):
            if e is not None and 0 < (s - e) / 60.0 <= BREAK_MAX_MIN:
                gaps.append((s - e) / 60.0)
    out = {"days_seen": len(days),
           "usual_start": round(_median(starts), 2)}
    if len(ends) >= min_days:
        out["usual_end"] = round(_median(ends), 2)
    if lengths:
        out["usual_session_min"] = round(_median(lengths), 1)
    if len(gaps) >= min_days:
        out["usual_break_min"] = round(_median(gaps), 1)
    return out
