"""Presence decoys and the latest samples, numbers only (run on the MSI).

    python decoy_check.py [n]
"""
import os
import sqlite3
import sys
from datetime import datetime

n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
db = sqlite3.connect(os.path.join(os.environ.get("USERPROFILE", "."),
                                  "avi-remote", "presence-dev.db"))
print("decoys:")
for r in db.execute("SELECT id, box, learned_at, seen_at, hits, moved "
                    "FROM decoys"):
    print(" ", r[0], r[1], datetime.fromtimestamp(r[2]).strftime("%H:%M:%S"),
          r[3] and datetime.fromtimestamp(r[3]).strftime("%H:%M:%S"),
          "hits", r[4], "moved", r[5])
print("samples:")
for ts, present, conf, view_ok, box, light in db.execute(
        "SELECT ts, present, conf, view_ok, box, light FROM samples "
        "ORDER BY ts DESC LIMIT ?", (n,)):
    print(" ", datetime.fromtimestamp(ts).strftime("%H:%M:%S"), present,
          conf and round(conf, 3), view_ok, box, light)
