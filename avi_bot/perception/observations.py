"""What AVI has noticed about him, as sentences it can say.

The main directive is to learn about Shaurya by watching his surroundings and
use it to help. Presence and the desk layout were being learned, but asked
"what do you know about me" the brain answered only from what he had TOLD it
-- so everything learned by watching was invisible to him. These are the
watched half, phrased as things AVI noticed rather than facts it was given,
and only as confident as the evidence: one day of arrivals is not a habit
(presence.summary withholds usual_arrival_hour until there are three).
"""
from avi_bot import numbers


def notes(presence=None, layout=None):
    """Short sentences, most personal first. Either input may be None."""
    out = []
    p = presence or {}
    if p.get("here") and p.get("for_minutes"):
        out.append(f"You've been at the desk {int(p['for_minutes'])} "
                   "minutes.")
    elif p.get("today_minutes"):
        out.append(f"You were at the desk {int(p['today_minutes'])} "
                   "minutes today.")
    if p.get("usual_arrival_hour") is not None:
        out.append(f"You usually sit down around "
                   f"{_clock(p['usual_arrival_hour'])}.")
    if p.get("usual_leave_hour") is not None:
        out.append(f"You usually finish around "
                   f"{_clock(p['usual_leave_hour'])}.")
    if p.get("usual_session_min"):
        out.append(f"You usually sit for about "
                   f"{_duration(p['usual_session_min'])} at a time.")
    if p.get("usual_break_min"):
        out.append(f"Your breaks usually last about "
                   f"{_duration(p['usual_break_min'])}.")
    if (p.get("breaks_judged") or 0) >= 3:
        out.append(f"You took {p.get('breaks_taken', 0)} of the last "
                   f"{p['breaks_judged']} breaks I suggested.")
    for o in layout or []:
        where = (o.get("where") or "").rstrip(".")
        if where and o.get("label"):
            out.append(f"Your {o['label']} was {where[0].lower()}{where[1:]}.")
    return out


def _clock(hour):
    """An hour of the day (24.5 = 00:30 next morning) as words."""
    import datetime as dt
    m = int(round(float(hour) * 60)) % (24 * 60)
    at = dt.datetime(2000, 1, 1, m // 60, m % 60)
    return numbers.clock_time(at, am_pm=True)


def _duration(minutes):
    m = int(round(float(minutes)))
    if m < 60:
        return f"{m} minutes"
    h, r = divmod(m, 60)
    # Nearest quarter hour reads like a person; "1 hour 7 minutes" does not.
    q = int(round(r / 15.0)) * 15
    if q == 60:
        h, q = h + 1, 0
    hours = f"{h} hour" + ("s" if h != 1 else "")
    return hours if not q else f"{hours} {q} minutes"


def about(query, sentences):
    """The noticed sentences that mention `query` (all of them if None)."""
    if not query:
        return list(sentences)
    q = query.lower().strip()
    words = [w for w in q.replace("?", "").split() if len(w) > 2]
    return [s for s in sentences
            if any(w in s.lower() for w in words or [q])]
