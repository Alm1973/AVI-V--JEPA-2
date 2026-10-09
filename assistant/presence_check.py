"""Presence samples by hour: how often AVI thought he was there, how sure.

Run ON THE MSI. Read-only; numbers only.   python presence_check.py [day]
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

db = sqlite3.connect(str(Path.home() / "avi-remote" / "presence-dev.db"))
day = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
rows = db.execute("SELECT ts, present, conf, view_ok FROM samples "
                  "ORDER BY ts").fetchall()
by = {}
for ts, pr, conf, ok in rows:
    t = datetime.fromtimestamp(ts)
    if t.strftime("%Y-%m-%d") != day:
        continue
    h = by.setdefault(t.hour, {"n": 0, "yes": 0, "view": 0, "confs": []})
    h["n"] += 1
    h["yes"] += pr
    h["view"] += ok
    if conf:
        h["confs"].append(conf)
for hr in sorted(by):
    h = by[hr]
    c = sorted(h["confs"])
    med = c[len(c) // 2] if c else None
    print(f"{hr:02d}h  samples {h['n']:3d}  present {h['yes']:3d}  "
          f"seat-view {h['view']:3d}  median conf {med}")
