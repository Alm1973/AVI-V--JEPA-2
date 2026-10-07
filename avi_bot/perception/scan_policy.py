"""When AVI may look over the desk on its own.

Desk changes are learned from consecutive scans, so a habit like "the
backpack goes when he leaves" needs scans at the right moments -- not only
when someone remembers to POST /desk_scan. Since surveys go column by column
a scan costs ~1/24 of the base travel it used to, so a scheduled one is
affordable on the servo that stalls.

OFF unless switched on (dashboard-settings.json "desk_scan_every_min"):
moving on its own in his room is his call. When on, the rules are:
  * never while he is at the desk -- the arm sweeping around is a distraction,
    and the seat view is how presence is measured;
  * only in waking hours;
  * at most once per interval, measured from the last scan of any kind.
"""

DEFAULT_HOURS = (7, 23)        # local hours, [start, end)


def due(now_ts, last_scan_ts, every_min, here, hour, hours=DEFAULT_HOURS):
    """(should_scan, why). `hour` is the local hour of `now_ts`."""
    if not every_min or every_min <= 0:
        return False, "scheduled scans are off"
    if here:
        return False, "he is at the desk"
    start, end = hours
    if not (start <= hour < end):
        return False, f"outside {start}:00-{end}:00"
    if last_scan_ts is not None and now_ts - last_scan_ts < every_min * 60:
        left = (every_min * 60 - (now_ts - last_scan_ts)) / 60.0
        return False, f"next in {left:.0f} min"
    return True, "due"
