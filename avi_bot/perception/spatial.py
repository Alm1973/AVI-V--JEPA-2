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
    others = [o for o in objects if o is not me]
    if not others:
        return None
    anchor = None
    for want in landmarks:
        anchor = next((o for o in others if o["label"] == want), None)
        if anchor is not None:
            break
    if anchor is None:
        anchor = min(others, key=lambda o: math.hypot(o["x"] - me["x"],
                                                      o["y"] - me["y"]))
    rel = relation(me, anchor)
    nearer = range_from_me(me) < range_from_me(anchor)
    tail = "" if rel["words"].startswith("next to") else (
        ", nearer to me" if nearer else ", farther from me")
    return f"{rel['words'].capitalize()}{tail}."


def layout(objects):
    """Every object's range from AVI and its place relative to each other,
    for /desk_layout. Objects need "label", "x", "y"."""
    out = []
    for o in objects:
        out.append({"label": o["label"], "x": round(o["x"], 2),
                    "y": round(o["y"], 2),
                    "range": round(range_from_me(o), 2),
                    "where": describe(o, objects)})
    return sorted(out, key=lambda r: r["range"])
