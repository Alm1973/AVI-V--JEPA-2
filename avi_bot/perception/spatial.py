"""Where things are relative to each other, in words.

world.on_plane gives every desk object a position in units of the lens's
height above the desk, origin on the base axis, x straight ahead (base_yaw
centre), y to AVI's right. This turns pairs of positions into what a person
would say: "left of the laptop, nearer to me".

Two limits are kept visible rather than papered over:
  * Units. Until the lens height is measured, distances are RELATIVE -- "twice
    as far as the pen" is real, "40 cm" is not. `metres()` refuses without
    the height.
  * Viewpoint. Left and right are AVI's, looking out from its base. Where
    Shaurya sits is not measured, and guessing his left would be wrong half
    the time.
"""
import math

# Closer than this (lens heights) and two objects are "next to" each other;
# direction words would be noise at that range.
NEXT_TO = 0.35
# A side has to dominate by this ratio before it is named ("left", not
# "left-ish and in front").
DOMINANT = 1.5


def metres(dist_h, lens_height_m):
    """Convert lens heights to metres. Raises without a measured height."""
    if not lens_height_m:
        raise ValueError("lens height above the desk is unmeasured; "
                         "distances are only relative")
    return dist_h * float(lens_height_m)


def relation(a, b):
    """Where `a` is relative to `b`, from AVI's point of view.

    a and b are {"label", "x", "y"}. Returns {"words", "dx", "dy", "dist"}:
    dx > 0 means a is FARTHER from AVI than b, dy > 0 means a is to AVI's
    RIGHT of b.
    """
    dx, dy = a["x"] - b["x"], a["y"] - b["y"]
    dist = math.hypot(dx, dy)
    if dist < NEXT_TO:
        words = f"next to the {b['label']}"
    else:
        side = ("right of" if dy > 0 else "left of")
        depth = ("behind" if dx > 0 else "in front of")
        if abs(dy) >= DOMINANT * abs(dx):
            words = f"{side} the {b['label']}"
        elif abs(dx) >= DOMINANT * abs(dy):
            words = f"{depth} the {b['label']}"
        else:
            words = f"{depth} and {side} the {b['label']}"
    return {"words": words, "dx": round(dx, 2), "dy": round(dy, 2),
            "dist": round(dist, 2)}


def range_from_me(obj):
    """Distance from the base axis, lens heights."""
    return math.hypot(obj["x"], obj["y"])


def describe(target, objects, landmarks=("laptop", "mug", "book")):
    """One sentence placing `target` among the other desk objects.

    `target` is one of `objects`, or a label (then the object of that label
    seen most often). Passing the object matters when two share a label:
    looking the second backpack up by name described the first one.
    Anchors on the most familiar landmark present (the laptop first: it is
    what he is looking at most of the day), else on the nearest other
    object. Returns None if the target has no desk position.
    """
    if isinstance(target, dict):
        me = target
    else:
        same = [o for o in objects if o["label"] == target]
        me = max(same, key=lambda o: o.get("hits", 0)) if same else None
    if me is None:
        return None
    anchor = _anchor(me, objects, landmarks)
    if anchor is None:
        return None
    rel = relation(me, anchor)
    nearer = range_from_me(me) < range_from_me(anchor)
    tail = "" if rel["words"].startswith("next to") else (
        ", nearer to me" if nearer else ", farther from me")
    return f"{rel['words'].capitalize()}{tail}."


def _anchor(me, objects, landmarks=("laptop", "mug", "book")):
    """What to place `me` against: the most familiar landmark present (the
    laptop first: it is what he looks at most of the day), else the
    nearest other object. None when it is alone."""
    others = [o for o in objects if o is not me]
    if not others:
        return None
    for want in landmarks:
        anchor = next((o for o in others if o["label"] == want), None)
        if anchor is not None:
            return anchor
    return min(others, key=lambda o: math.hypot(o["x"] - me["x"],
                                                o["y"] - me["y"]))


def where_words(label, objects, lens_height_m=0.0, age_s=None):
    """The answer to "where's my pen" / "how far is my pen", as ONE short
    sentence, most useful part first: "Left of the laptop, 0.8 m from me,
    as of 31 h ago." Replies are capped at 20 words and cut at a sentence;
    built as finder text + place + distance, the distance was the part cut
    (robot, 2026-10-08). None when `label` has no desk position."""
    same = [o for o in objects if o["label"] == label and "x" in o]
    if not same:
        return None
    me = max(same, key=lambda o: o.get("hits", 0))
    parts = []
    anchor = _anchor(me, [o for o in objects if "x" in o])
    if anchor is not None:
        parts.append(relation(me, anchor)["words"])
    if lens_height_m:
        parts.append(f"{metres(range_from_me(me), lens_height_m):.1f} m "
                     "from me")
    if not parts:
        return None
    s = ", ".join(parts)
    if age_s is not None:
        s += f", as of {ago(age_s)}"
    return s[0].upper() + s[1:] + "."


def layout(objects, lens_height_m=0.0):
    """Every object's range from AVI and its place relative to each other,
    for /desk_layout. Objects need "label", "x", "y". With a measured lens
    height, metric fields are added alongside the relative ones."""
    out = []
    for o in objects:
        row = {"label": o["label"], "x": round(o["x"], 2),
               "y": round(o["y"], 2), "height": o.get("height"),
               "range": round(range_from_me(o), 2),
               "where": describe(o, objects)}
        if lens_height_m:
            row["range_m"] = round(metres(row["range"], lens_height_m), 2)
            if row["height"] is not None:
                row["height_m"] = round(metres(row["height"],
                                               lens_height_m), 2)
        out.append(row)
    return sorted(out, key=lambda r: r["range"])


def ago(seconds):
    """'12 min ago', '5 h ago', '2 days ago' -- how old a scan is, in the
    words he'd use."""
    s = max(0.0, float(seconds))
    if s < 90:
        return "just now"
    if s < 90 * 60:
        return f"{s / 60:.0f} min ago"
    if s < 36 * 3600:
        return f"{s / 3600:.0f} h ago"
    return f"{s / 86400:.0f} days ago"


def desk_sentence(objects, lens_height_m, age_s, limit=6):
    """The desk as the model is told it: each placed object's distance from
    AVI, nearest first, and how old that is. The prompt promised the model
    "where things are on the desk" and sent only names, so asked "how far
    is my pen" it made up 0.35 m while the scan said 0.76 (2026-10-08).
    None when nothing is placed or the lens height is unmeasured."""
    if not lens_height_m:
        return None
    rows = layout([o for o in objects if "x" in o], lens_height_m)
    seen, parts = set(), []
    for r in rows:
        if r["label"] in seen or r["label"] == "person":
            continue
        seen.add(r["label"])
        parts.append(f"{r['label']} {r['range_m']:.1f} m")
    if not parts:
        return None
    return (f"Desk at the last scan ({ago(age_s)}; things may have moved "
            "since), distance from you: " + ", ".join(parts[:limit]) + ".")
