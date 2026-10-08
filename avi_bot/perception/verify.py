"""Did the arm do what it was told? Judged from the picture, not the command.

The language model turns what he says into joint targets; nothing checked
that the camera then actually moved, or moved the right way. Commands went
out to servos with no power and the robot reported success.

Two independent judges, both on a frame before and a frame after the move:

  ORB  -- perception/horizon.register: how the picture's content slid at the
          image centre, in pixels. Precise, but needs texture.
  JEPA -- V-JEPA 2's spatial tokens. The encoder's patch features of the
          after-frame are matched against the before-frame's at every shift;
          the best shift is how the scene moved, in tokens. Coarse (a token
          is 1/16 of the frame each way) but it reads the scene through the
          same learned representation the rest of AVI's learning uses.

Both are turned into which way the CAMERA moved. Content sliding LEFT means
the camera turned RIGHT; content sliding DOWN means it tilted UP (image y
grows downward). Measured sign for the base: world.py, "+8 deg turn slides
the picture left".
"""
import numpy as np

DIRECTIONS = ("left", "right", "up", "down")
# Smaller content motion than this (px at the frame's own size) is "none":
# a still scene registers within a few px, and a 5 deg turn is ~100 px.
MIN_PX = 25.0
# A shift must leave at least this fraction of the token grid overlapping to
# be scored; at the edges of the search the overlap is a sliver.
MIN_OVERLAP = 0.35


def camera_direction(dx, dy, min_px=MIN_PX):
    """Which way the camera moved, from how the content moved (px)."""
    if max(abs(dx), abs(dy)) < min_px:
        return "none"
    if abs(dx) >= abs(dy):
        return "left" if dx > 0 else "right"
    return "up" if dy > 0 else "down"


def _unit(g):
    g = np.asarray(g, dtype=np.float32)
    return g / (np.linalg.norm(g, axis=-1, keepdims=True) + 1e-8)


def _score(a, b, dx, dy):
    """Mean cosine of a[y, x] against b[y + dy, x + dx] over the overlap."""
    h, w = a.shape[:2]
    y0, y1 = max(0, -dy), min(h, h - dy)
    x0, x1 = max(0, -dx), min(w, w - dx)
    if (y1 - y0) * (x1 - x0) < MIN_OVERLAP * h * w:
        return None
    pa = a[y0:y1, x0:x1]
    pb = b[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
    return float((pa * pb).sum(-1).mean())


def _refine(m, c, p):
    """Sub-token offset of a peak from its two neighbours (parabola)."""
    if m is None or p is None:
        return 0.0
    den = m - 2.0 * c + p
    return 0.0 if abs(den) < 1e-9 else float(np.clip(0.5 * (m - p) / den,
                                                     -0.5, 0.5))


# The camera's 16:9 frame is squashed into V-JEPA's square 16x16 grid, so a
# token row is 45 px of a 720-px frame and a 12-degree tilt moves ~6.7 rows.
# A +-6 search could not see it: "up" came back as "none" on the robot
# (2026-10-08). +-9 still leaves 7/16 of the grid overlapping.
MAX_SHIFT = 9
# Two grids this alike at zero shift are the same view: nothing moved.
# Robot, 2026-10-08: no-move controls scored 0.92; 6-degree tilts 0.81,
# 12- and 20-degree moves 0.67-0.73.
STILL = 0.88
# The best shift must stand this far above the median shift's score.
# Robot, 2026-10-08, one-axis search: every real 6-20 degree move scored
# 0.08-0.17; no-move controls are caught earlier, by STILL.
MIN_PROMINENCE = 0.06


def token_shift(a, b, max_shift=MAX_SHIFT, axis=None, band=1):
    """How the scene moved from token grid `a` to `b`, both (H, W, D).

    Returns {"dx", "dy"} in tokens (content in `a` at x appears in `b` at
    x + dx), "still" (the same view, unmoved), "edge" (the best shift is on
    the search boundary: the real one may be beyond it), "confident", the
    peak's "prominence", and the score profiles through zero on each axis.

    V-JEPA's tokens carry their GRID POSITION: a token resembles whatever
    sat at the same spot in another frame, whatever it shows. Matching the
    raw features made "no shift" a peak as high as the true one (robot,
    2026-10-08: a 12-degree tilt up scored 0.729 at zero, 0.735 at +6.6
    rows). So each shift is scored against the frames' match with
    THEMSELVES at that shift:

        score(s) = S_ab(s) - (S_aa(s) + S_bb(s)) / 2

    The position signal enters all three terms identically and cancels;
    the content enters S_ab at lag s - s0 but the self terms at lag s, so
    the score peaks at the true shift s0 -- and only there. (Matching the
    frame DIFFERENCE also cancels position but peaks at +s0 and -s0 alike,
    and on the robot it turned horizontal turns into diagonals.)

    `axis` ("x" or "y") searches along one axis, +-`band` across it: a base
    turn slides the picture sideways and a wrist tilt slides it vertically,
    so the judge knows WHICH joint moved -- not which way or how far, which
    is what it measures. Unconstrained, 20-degree pans came back with ghost
    vertical components of 4-5 rows on the robot.
    """
    ua, ub = _unit(a), _unit(b)
    raw0 = float((ua * ub).sum(-1).mean())
    if raw0 >= STILL:
        return {"dx": 0.0, "dy": 0.0, "still": True, "edge": False,
                "confident": True, "prominence": None,
                "raw0": round(raw0, 4), "profile_x": None, "profile_y": None}
    scores = {}
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            if axis == "x" and abs(dy) > band:
                continue
            if axis == "y" and abs(dx) > band:
                continue
            ab = _score(ua, ub, dx, dy)
            if ab is None:
                continue
            aa, bb = _score(ua, ua, dx, dy), _score(ub, ub, dx, dy)
            scores[(dx, dy)] = ab - (aa + bb) / 2.0
    (bx, by), best = max(scores.items(), key=lambda kv: kv[1])
    rest = sorted(scores.values())
    prominence = best - rest[len(rest) // 2]
    fx = bx + _refine(scores.get((bx - 1, by)), best, scores.get((bx + 1, by)))
    fy = by + _refine(scores.get((bx, by - 1)), best, scores.get((bx, by + 1)))
    edge = abs(bx) >= max_shift or abs(by) >= max_shift

    def prof(along):
        out = []
        for k in range(-max_shift, max_shift + 1):
            v = scores.get((k, 0) if along == "x" else (0, k))
            out.append(None if v is None else round(v, 3))
        return out
    return {"dx": round(fx, 2), "dy": round(fy, 2), "still": False,
            "edge": edge, "prominence": round(prominence, 4),
            "confident": not edge and prominence >= MIN_PROMINENCE,
            "raw0": round(raw0, 4), "score": round(best, 4),
            "profile_x": prof("x"), "profile_y": prof("y")}


AXIS = {"left": "x", "right": "x", "up": "y", "down": "y"}


def judge(commanded, orb_px=None, jepa_tokens=None, token_px=(80.0, 45.0),
          min_px=MIN_PX):
    """The verdict on one move.

    commanded: "left" / "right" / "up" / "down".
    orb_px: (dx, dy) content motion from register(), or None if unmeasurable.
    jepa_tokens: (dx, dy) from token_shift(), or None without the encoder.
    token_px: one token's size in frame pixels (width/16, height/16).

    "agrees" is True only when every judge that could see said the camera
    went the commanded way; "judged" is False when neither could see.
    """
    votes, motion = {}, {}
    if orb_px is not None:
        motion["orb_px"] = [round(float(orb_px[0]), 1),
                            round(float(orb_px[1]), 1)]
        votes["orb"] = camera_direction(orb_px[0], orb_px[1], min_px)
    if jepa_tokens is not None:
        px = (jepa_tokens[0] * token_px[0], jepa_tokens[1] * token_px[1])
        motion["jepa_px"] = [round(px[0], 1), round(px[1], 1)]
        votes["jepa"] = camera_direction(px[0], px[1], min_px)
    seen = list(votes.values())
    return {"commanded": commanded, "votes": votes, "motion": motion,
            "judged": bool(seen),
            "agrees": bool(seen) and all(v == commanded for v in seen)}


def dominant(before, after):
    """The direction a joint change should move the camera, for judging
    move_joints: the larger of the base turn and the wrist tilt."""
    dyaw = float(after.get("base_yaw", 0.0)) - float(before.get("base_yaw",
                                                                 0.0))
    dwr = float(after.get("wrist", 0.0)) - float(before.get("wrist", 0.0))
    if max(abs(dyaw), abs(dwr)) < 2.0:
        return None
    if abs(dyaw) >= abs(dwr):
        return "right" if dyaw > 0 else "left"
    return "up" if dwr > 0 else "down"


def sentence(verdict):
    """What AVI says when the picture disagrees with the command; None when
    it agrees or could not be judged."""
    if not verdict or not verdict.get("judged") or verdict.get("agrees"):
        return None
    seen = set(verdict["votes"].values())
    went = seen.pop() if len(seen) == 1 else None
    want = verdict["commanded"]
    if went == "none":
        return f"I tried to look {want}, but the camera didn't move."
    if went:
        return f"I tried to look {want}, but the camera went {went}."
    return f"I'm not sure I moved {want}; my two checks disagree."
