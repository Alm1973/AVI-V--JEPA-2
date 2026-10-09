"""AVI learning what its own joints do, by moving them and watching.

The question Shaurya asked (2026-10-09): would a 3D model of the robot give
JEPA full control of the body? The research (docs/RESEARCH-motion.md) says
the body model does not go INTO JEPA -- V-JEPA 2-AC learns what actions do
from video plus the arm's own state, and Lipson's robots learn a model of
themselves by moving and watching. So this is the experiment, done the same
way: "motor babbling". Small single-joint moves from one pose, each measured
by ORB (pixels) and V-JEPA (token shift), give a learned image Jacobian --
how far the picture slides per degree of each joint. Then the three
predictors are compared on moves they did not see:

    old rule   base_yaw -> sideways, wrist -> up/down; shoulder and elbow
               predict nothing (verify.dominant before the body model)
    3D model   motion/body.py: orientation of the camera from all four
               joints, converted to pixels by the lens
    learned    the slopes fitted from the other moves (leave-one-out)

If the 3D model's direction for shoulder and elbow agrees with the picture,
it is safe to let the move judge use them (body.reach_judged). The learned
slopes are the self-model: they include what the 3D model cannot know
(backlash, the servos' real travel, parallax at this pose).

Pure numbers here; the moving and measuring is dashboard/server.py
babble(). Frames never leave the MSI.
"""
import math
import random

from avi_bot.motion import poses

# Picture pixels per degree of camera turn, both axes, at 1280x720: the
# [horizon] fov_deg 48.6 is in the linear convention (frame width per
# degree at the centre), and the C270's pixels are square.
PX_PER_DEG = 1280.0 / 48.6
JOINTS = poses.JOINTS


def plan(start, n, max_deg, limits, body=None, seed=0, min_deg=3.0,
         edge=3.0):
    """n single-joint moves from `start`: (joint, delta), the joints taken
    in turn, sizes random in [min_deg, max_deg] with alternating signs.
    A delta that would leave the soft limits flips sign or shrinks; one
    the body model calls unsafe (where it trusts the start) is skipped."""
    rng = random.Random(seed)
    out = []
    k = 0
    tries = 0
    while len(out) < n and tries < n * 10:
        tries += 1
        j = JOINTS[k % len(JOINTS)]
        k += 1
        size = rng.uniform(min_deg, max_deg)
        sign = 1.0 if (len(out) // len(JOINTS)) % 2 == 0 else -1.0
        lo, hi = limits(j)
        here = float(start[j])
        d = sign * size
        if not (lo + edge <= here + d <= hi - edge):
            d = -d
        if not (lo + edge <= here + d <= hi - edge):
            room = max(hi - edge - here, here - lo - edge)
            if room < min_deg:
                continue
            d = room if here + room <= hi - edge else -room
        to = dict(start, **{j: here + d})
        if body is not None and body.safe(start) and not body.safe(to):
            continue
        out.append((j, round(d, 2)))
    return out


def predicted_px(body, before, after):
    """(dx, dy) the 3D model expects the CONTENT to slide, pixels: the
    camera turning right slides content left, tilting up slides it down
    (image y grows downward)."""
    daz, del_ = body.view_change(before, after)
    return -daz * PX_PER_DEG, del_ * PX_PER_DEG


def old_rule_px(before, after, yaw_px=-15.26, wrist_px=22.33):
    """What the judge could predict before the body model: base_yaw and
    wrist from their measured slopes ([camera] in joints.toml), nothing for
    shoulder or elbow."""
    dy = float(after["base_yaw"]) - float(before["base_yaw"])
    dw = float(after["wrist"]) - float(before["wrist"])
    return dy * yaw_px, dw * wrist_px


def _measured(row, which):
    m = row.get(which)
    if not m:
        return None
    return float(m["dx"]), float(m["dy"])


def slopes(rows, which="orb", skip=None):
    """Least-squares px per degree for each joint, through the origin,
    from single-joint rows: {joint: {"dx", "dy", "n", "r2"}}."""
    out = {}
    for j in JOINTS:
        xs, ys = [], []
        for i, r in enumerate(rows):
            if i == skip or r["joint"] != j:
                continue
            m = _measured(r, which)
            if m is None:
                continue
            xs.append(float(r["delta"]))
            ys.append(m)
        if not xs:
            continue
        sxx = sum(x * x for x in xs)
        if sxx <= 0:
            continue
        kx = sum(x * y[0] for x, y in zip(xs, ys)) / sxx
        ky = sum(x * y[1] for x, y in zip(xs, ys)) / sxx
        ss = sum((y[0] - kx * x) ** 2 + (y[1] - ky * x) ** 2
                 for x, y in zip(xs, ys))
        st = sum(y[0] ** 2 + y[1] ** 2 for y in ys)
        out[j] = {"dx": round(kx, 2), "dy": round(ky, 2), "n": len(xs),
                  "r2": round(1.0 - ss / st, 3) if st > 0 else None}
    return out


def _dirword(dx, dy, min_px=25.0):
    """Which way the CAMERA went, from content motion (verify's rule)."""
    if max(abs(dx), abs(dy)) < min_px:
        return "none"
    if abs(dx) >= abs(dy):
        return "right" if dx < 0 else "left"
    return "up" if dy > 0 else "down"


def compare(rows, body, which="orb", min_px=25.0):
    """Per joint, how well each predictor did on the moves: mean pixel
    error, and how often its direction word matched the picture's.

    {"joints": {joint: {"n", "measured_px_per_deg", "model": {...},
    "old_rule": {...}, "learned": {...}}}, "verdict": {joint: str}}"""
    learned_all = slopes(rows, which)
    per = {}
    for i, r in enumerate(rows):
        m = _measured(r, which)
        if m is None:
            continue
        before = r["pose"]
        after = dict(before, **{r["joint"]: before[r["joint"]]
                                + r["delta"]})
        loo = slopes(rows, which, skip=i).get(r["joint"])
        preds = {
            "model": predicted_px(body, before, after) if body else None,
            "old_rule": old_rule_px(before, after),
            "learned": ((loo["dx"] * r["delta"], loo["dy"] * r["delta"])
                        if loo else None),
        }
        seen = _dirword(m[0], m[1], min_px)
        d = per.setdefault(r["joint"], {"n": 0, "errs": {}, "hits": {}})
        d["n"] += 1
        for k, p in preds.items():
            if p is None:
                continue
            d["errs"].setdefault(k, []).append(math.hypot(p[0] - m[0],
                                                          p[1] - m[1]))
            want = _dirword(p[0], p[1], min_px)
            d["hits"].setdefault(k, []).append(want == seen and
                                               seen != "none")
    out, verdict = {}, {}
    for j, d in per.items():
        row = {"n": d["n"], "measured_px_per_deg": learned_all.get(j)}
        for k in ("model", "old_rule", "learned"):
            if k in d["errs"]:
                e, h = d["errs"][k], d["hits"][k]
                row[k] = {"mean_err_px": round(sum(e) / len(e), 1),
                          "direction_right": f"{sum(h)}/{len(h)}"}
        out[j] = row
        verdict[j] = _verdict(row)
    return {"joints": out, "verdict": verdict, "which": which}


def _verdict(row):
    """One line: which predictor this joint should be judged with."""
    def frac(k):
        got = row.get(k, {}).get("direction_right")
        if not got:
            return 0.0
        a, b = got.split("/")
        return int(a) / int(b) if int(b) else 0.0
    best = min((k for k in ("model", "old_rule", "learned") if k in row),
               key=lambda k: row[k]["mean_err_px"], default=None)
    return (f"model directions {frac('model'):.0%}, old rule "
            f"{frac('old_rule'):.0%}, learned {frac('learned'):.0%}; "
            f"closest in pixels: {best}")


# --- the daily self-check: is every joint still doing what it learned? ---
# Lipson's self-modelling robots use the model to notice when the body has
# stopped matching it (a damaged leg). AVI's version: once a day, while he
# is away and the room is lit, one small move per joint, out and back,
# compared with the slopes it learned. A joint that no longer moves the
# picture -- a loose horn, an unplugged servo, a browned-out supply -- is
# something to tell him, rather than something every later move silently
# gets wrong.
CHECK_HOURS = (9, 15)          # [from, to): when he is at school
CHECK_DEG = 4.0
CHECK_MIN_LIGHT = 60.0         # ORB needs a lit room
STUCK_FRAC = 0.3               # under 30% of the learned slide = not moving


def check_due(today, last_day, here, light, hour, enabled=True):
    """(due, why). `today`/`last_day` are date strings."""
    if not enabled:
        return False, "switched off"
    if last_day == today:
        return False, "already done today"
    if not CHECK_HOURS[0] <= hour < CHECK_HOURS[1]:
        return False, "outside the check hours"
    if here:
        return False, "he is here"
    if light is None or light < CHECK_MIN_LIGHT:
        return False, "too dark to see the moves"
    return True, "due"


def expected(history, body=None, pose=None):
    """{joint: (dx, dy) px per degree} to hold a check against: what the
    earlier babbling learned, else what the body model predicts."""
    got = {j: (v["dx"], v["dy"]) for j, v in slopes(history).items()
           if v["n"] >= 2}
    if body is not None and pose is not None:
        for j in JOINTS:
            if j not in got:
                to = dict(pose, **{j: float(pose[j]) + 1.0})
                got[j] = predicted_px(body, pose, to)
    return got


def judge_check(rows, expect, frac=STUCK_FRAC, which="orb"):
    """{joint: {"ratio", "ok", "n"}}: the share of the expected slide each
    joint produced (median over its rows). ok None when the camera could
    not measure it."""
    out = {}
    for j in JOINTS:
        e = expect.get(j)
        ratios = []
        for r in rows:
            if r["joint"] != j or not e:
                continue
            m = _measured(r, which) or _measured(r, "jepa")
            ee = e[0] ** 2 + e[1] ** 2
            if m is None or ee <= 0 or not r["delta"]:
                continue
            ratios.append((m[0] * e[0] + m[1] * e[1])
                          / (float(r["delta"]) * ee))
        if not ratios:
            out[j] = {"ratio": None, "ok": None, "n": 0}
            continue
        ratios.sort()
        mid = ratios[len(ratios) // 2]
        out[j] = {"ratio": round(mid, 2), "ok": mid >= frac,
                  "n": len(ratios)}
    return out


_SAY = {"base_yaw": "base", "shoulder": "shoulder", "elbow": "elbow",
        "wrist": "camera tilt"}


def check_words(verdicts):
    """What to tell him, or None when every joint it could see moved."""
    bad = [_SAY[j] for j in JOINTS if verdicts.get(j, {}).get("ok") is False]
    if not bad:
        return None
    what = bad[0] if len(bad) == 1 else ", ".join(bad[:-1]) + " and " + bad[-1]
    return (f"My {what} didn't move in today's self-check. A servo may be "
            "loose or unpowered.")
