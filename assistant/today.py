"""Print a day's timeline and where objects were last seen.

    python today.py                 # today
    python today.py 2026-10-02      # another day
    python today.py --forget        # delete today's data (asks first)
"""
import argparse
from collections import defaultdict
from datetime import datetime

import settings
from memory import Memory


def hm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=datetime.now().date().isoformat())
    ap.add_argument("--db", default=settings.DB_PATH)
    ap.add_argument("--forget", action="store_true")
    args = ap.parse_args()
    mem = Memory(args.db)

    if args.forget:
        if input(f"Delete everything logged on {args.date}? Type yes: ") == "yes":
            mem.forget_day(args.date)
            print("Deleted.")
        return

    obs = mem.observations(args.date)
    sessions = mem.rebuild_sessions(args.date)
    if not obs:
        print(f"No observations on {args.date}.")
        return

    uncertain = sum(1 for o in obs if o["activity"] == settings.UNCERTAIN)
    print(f"{args.date}: {len(obs)} observations, "
          f"{uncertain} uncertain ({100 * uncertain / len(obs):.0f}%)\n")

    print("Timeline")
    totals = defaultdict(float)
    for s in sessions:
        minutes = (s["end_ts"] - s["start_ts"]) / 60
        totals[s["activity"]] += minutes
        print(f"  {hm(s['start_ts'])}-{hm(s['end_ts'])}  {s['activity']:<20}"
              f" {minutes:5.0f} min  ({s['n_obs']} obs)")

    print("\nTotals")
    for activity, minutes in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {activity:<20} {minutes / 60:4.1f} h")

    print("\nLast seen (verified by the VLM)")
    for r in mem.last_seen_all():
        when = datetime.fromtimestamp(r["ts"]).strftime("%a %H:%M")
        print(f"  {r['label']:<12} {when}, {r['zone']} side of the view")
    mem.close()


if __name__ == "__main__":
    main()
