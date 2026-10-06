"""What changed on his desk since the last look.

Presence says WHEN Shaurya is here. The other half of learning about him from
his surroundings is WHAT CHANGES around him: the backpack that is gone when he
has left, the mug that turns up in the morning, the book that moves from one
side of the desk to the other. Over days those changes are habits AVI can infer
-- "he leaves with his backpack around 7:30" -- without being told any of it.

The room map is the wrong thing to diff for this. It merges sightings and lets
them decay over hours, which is right for "where does the pen usually sit" and
wrong for "is the pen there NOW". So each desk scan takes a SNAPSHOT of what the
survey actually saw, and consecutive snapshots are compared.

One rule decides whether a comparison is honest: an object has only
DISAPPEARED if the new scan looked where it used to be. A scan that never
pointed at that part of the desk has no opinion, and reporting it as gone would
be inventing an absence -- the same mistake presence avoids by refusing to count
"not seen while looking at the ceiling" as "away".
"""
import json
import math
import sqlite3
import time

SAME_DEG = 12.0          # one object if two sightings are this close in view
MOVED_DEG = 15.0         # a matched object this far from before has moved
MIN_CONF = 0.30          # a detection weaker than this does not enter a snapshot
MIN_HITS = 2             # an object seen fewer times in one scan is a guess

PROFILES = {"dev": "desk-dev.db", "live": "desk.db"}

# Bump whenever what goes INTO a snapshot changes. Scans are only compared
# with scans of the same version: the first real comparison crossed a code
# change (clipped boxes started being excluded between scan 1 and scan 2) and
# reported a phone, a wallet and a backpack as "disappeared" -- indistinguishable
# from objects that were simply now being filtered out. A diff across a change
# in the instrument measures the instrument.
#   2: clipped boxes excluded.
#   3: directions on the sphere (world.View) instead of flat constants, and
#      stops whose yaw backlash was not taken up are left out.
SNAPSHOT_VERSION = 3


def _gap(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def snapshot(sightings, same_deg=SAME_DEG, min_conf=MIN_CONF,
             min_hits=MIN_HITS):
    """Cluster one scan's sightings into objects.

    `sightings` is [(label, yaw, wrist, conf[, (x, y)]), ...] -- directions
    already worked out by the map's geometry, and optionally where the box's
    bottom edge meets the desk (world.on_plane, lens heights). Returns
    [{label, yaw, wrist, hits, conf[, x, y]}, ...]. An object seen once in a
    whole scan is left out: one box in thirty stops is more likely a flicker
    than a thing.
    """
    clusters = []
    for s in sightings:
        label, yaw, wrist, conf = s[:4]
        xy = s[4] if len(s) > 4 else None
        if conf < min_conf:
            continue
        for c in clusters:
            if c["label"] == label and _gap(
                    (c["yaw"], c["wrist"]), (yaw, wrist)) <= same_deg:
                n = c["hits"]
                c["yaw"] = (c["yaw"] * n + yaw) / (n + 1)
                c["wrist"] = (c["wrist"] * n + wrist) / (n + 1)
                c["hits"] = n + 1
                c["conf"] = max(c["conf"], conf)
                if xy is not None:
                    c["xys"].append(xy)
                break
        else:
            clusters.append({"label": label, "yaw": float(yaw),
                             "wrist": float(wrist), "hits": 1,
                             "conf": float(conf),
                             "xys": [xy] if xy is not None else []})
    out = []
    for c in clusters:
        if c["hits"] < min_hits:
            continue
        xys = c.pop("xys")
        o = dict(c, yaw=round(c["yaw"], 1), wrist=round(c["wrist"], 1),
                 conf=round(c["conf"], 3))
        if xys:
            # The MEDIAN: one box whose bottom edge was cut by a desk item
            # in front of it lands far behind, and a mean would follow it.
            xs = sorted(p[0] for p in xys)
            ys = sorted(p[1] for p in xys)
            o["x"], o["y"] = (round(xs[len(xs) // 2], 2),
                              round(ys[len(ys) // 2], 2))
        out.append(o)
    return out


def covered(direction, stops, half_width):
    """Did any stop of a scan have this direction inside its frame?

    `stops` are the (yaw, wrist) the camera pointed at; `half_width` is half
    the frame in (yaw, wrist) degrees.
    """
    hy, hw = half_width
    return any(abs(direction[0] - y) <= hy and abs(direction[1] - w) <= hw
               for y, w in stops)


def diff(before, after, after_stops, half_width, moved_deg=MOVED_DEG,
         same_deg=SAME_DEG):
    """appeared / disappeared / moved / stayed between two snapshots.

    `after_stops` and `half_width` describe what the NEW scan could see, so a
    missing object is only called gone when the new scan looked at its place.
    Objects that could not be checked are reported as `unchecked`.
    """
    out = {"appeared": [], "disappeared": [], "moved": [], "stayed": [],
           "unchecked": []}
    used = set()
    for b in before:
        match, best = None, None
        for i, a in enumerate(after):
            if i in used or a["label"] != b["label"]:
                continue
            d = _gap((a["yaw"], a["wrist"]), (b["yaw"], b["wrist"]))
            if best is None or d < best:
                match, best = i, d
        if match is not None and best <= max(moved_deg, same_deg) * 3:
            used.add(match)
            a = after[match]
            if best > moved_deg:
                out["moved"].append({"label": b["label"],
                                     "from": [b["yaw"], b["wrist"]],
                                     "to": [a["yaw"], a["wrist"]],
                                     "by_deg": round(best, 1)})
            else:
                out["stayed"].append(b["label"])
            continue
        if covered((b["yaw"], b["wrist"]), after_stops, half_width):
            out["disappeared"].append({"label": b["label"],
                                       "at": [b["yaw"], b["wrist"]]})
        else:
            out["unchecked"].append(b["label"])
    for i, a in enumerate(after):
        if i not in used:
            out["appeared"].append({"label": a["label"],
                                    "at": [a["yaw"], a["wrist"]]})
    return out


class DeskLog:
    """Snapshots and the changes between them, in a dev-profile sqlite."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS scans (
        ts REAL NOT NULL, objects TEXT NOT NULL, stops TEXT NOT NULL,
        half_width TEXT NOT NULL, here INTEGER, version INTEGER);
    CREATE TABLE IF NOT EXISTS changes (
        ts REAL NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
        detail TEXT, here INTEGER);
    """

    def __init__(self, path=":memory:", clock=time.time,
                 version=SNAPSHOT_VERSION):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(self.SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(scans)")}
        if "version" not in cols:
            # Scans from before versions existed are version 1 by definition.
            self.db.execute("ALTER TABLE scans ADD COLUMN version INTEGER")
            self.db.execute("UPDATE scans SET version = 1")
            self.db.commit()
        self.clock = clock
        self.version = int(version)

    def close(self):
        self.db.close()

    def last(self):
        """The latest scan of THIS snapshot version, or (None, None)."""
        row = self.db.execute(
            "SELECT ts, objects FROM scans WHERE version = ? "
            "ORDER BY ts DESC LIMIT 1", (self.version,)).fetchone()
        return (row[0], json.loads(row[1])) if row else (None, None)

    def record(self, objects, stops, half_width, here=None, at=None):
        """Store a scan and the changes since the previous one."""
        at = self.clock() if at is None else float(at)
        prev_ts, prev = self.last()
        self.db.execute("INSERT INTO scans VALUES (?,?,?,?,?,?)",
                        (at, json.dumps(objects), json.dumps(stops),
                         json.dumps(list(half_width)),
                         None if here is None else int(bool(here)),
                         self.version))
        changes = None
        if prev is not None:
            changes = diff(prev, objects, stops, half_width)
            for kind in ("appeared", "disappeared", "moved"):
                for c in changes[kind]:
                    self.db.execute(
                        "INSERT INTO changes VALUES (?,?,?,?,?)",
                        (at, kind, c["label"], json.dumps(c),
                         None if here is None else int(bool(here))))
            changes["since"] = prev_ts
        self.db.commit()
        return changes

    def history(self, label=None, limit=50):
        q = "SELECT ts, kind, label, detail, here FROM changes"
        args = []
        if label:
            q += " WHERE label = ?"
            args.append(label)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        return [{"ts": round(ts, 1), "kind": k, "label": lb,
                 "detail": json.loads(d) if d else None,
                 "here": None if h is None else bool(h)}
                for ts, k, lb, d, h in self.db.execute(q, args)]
