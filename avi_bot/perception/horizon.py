"""Where is level? Found from how a base turn ROLLS the picture.

Every direction AVI knows sits in the dome frame, which is anchored to TALL,
a pose he taught. That frame has no "up" in it. A camera looking down at the
desk and one looking at the wall can share an azimuth and an elevation from
TALL and still mean nothing about height, and every 3D question (how high,
how far across the desk, is it on the desk or the shelf) needs which way is
level.

The geometry gives it for free. Turn the base by dth about its axis while the
camera looks at elevation e above the plane perpendicular to that axis. In the
camera's own frame the turn splits into two parts:

    pan  (about the image's up axis):   g * cos(e) * dth   -> the image SLIDES
    roll (about the optical axis):      g * sin(e) * dth   -> the image ROTATES

Looking level, the picture slides and does not turn. Looking straight up, it
turns like a clock face and does not slide. So:

  * The ROLL, measured at several wrist tilts, traces g * sin(k (w - w_level)).
    Fitting it gives the wrist angle that looks level AND the joint's true
    gain g. Roll is an angle in the image, so it needs no field of view, and
    it is pure rotation, so it carries no parallax -- neither of the two
    numbers this project has been unable to pin down can leak into it.
  * The SLIDE traces g * cos(...) * (px per degree), plus parallax for near
    things. Against the roll it measures the field of view, which until now
    has been a datasheet number.

This also tests the long-standing reading in config/joints.toml that base_yaw
under-travels its command ~1.8x. A camera pitched ~50 degrees slides at
cos(50) = 0.64 of the turn with NO under-travel; the roll tells the two apart.
"""
import math

import numpy as np

# Below this many RANSAC inliers a registration is a guess, not a measurement.
MIN_INLIERS = 25
# A level whose two halves disagree by more than this (deg of roll per deg of
# turn) is reported but not fitted -- usually backlash or a blank wall.
MAX_HALF_DISAGREE = 0.08


class NotMeasurable(ValueError):
    pass


def decompose(affine, width, height):
    """A 2x3 partial-affine (rotation, uniform scale, translation) about the
    image ORIGIN, re-expressed about the image CENTRE.

    OpenCV fits x' = A x + t with x measured from the top-left corner. A pure
    roll about the centre then shows up as a large translation, so the slide
    has to be read at the centre: t_c = A c + t - c.

    Returns (roll_deg, dx_px, dy_px, scale). roll_deg is counter-clockwise in
    image coordinates (y down), i.e. positive = the picture turned clockwise
    as seen on a screen; only the sign convention has to be consistent.
    """
    a = np.asarray(affine, dtype=float)
    A, t = a[:, :2], a[:, 2]
    c = np.array([width / 2.0, height / 2.0])
    tc = A @ c + t - c
    scale = math.hypot(A[0, 0], A[1, 0])
    roll = math.degrees(math.atan2(A[1, 0], A[0, 0]))
    return roll, float(tc[0]), float(tc[1]), scale


def register(img_a, img_b, max_side=640):
    """How img_b sits relative to img_a: roll, slide at the centre, scale.

    ORB features, cross-checked matches, RANSAC similarity. Returns a dict, or
    raises NotMeasurable when the view has too little texture -- the blank
    wall at dome elevation 48-120 is exactly that, and a registration on it
    would be noise reported as a number.
    """
    import cv2
    h, w = img_a.shape[:2]
    f = min(1.0, max_side / float(max(h, w)))
    def prep(img):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        return cv2.resize(g, None, fx=f, fy=f) if f < 1.0 else g
    ga, gb = prep(img_a), prep(img_b)
    orb = cv2.ORB_create(2000)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db = orb.detectAndCompute(gb, None)
    if da is None or db is None or len(ka) < MIN_INLIERS or len(kb) < MIN_INLIERS:
        raise NotMeasurable("too few features")
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < MIN_INLIERS:
        raise NotMeasurable(f"only {len(matches)} matches")
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    M, mask = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC,
                                          ransacReprojThreshold=3.0)
    n = int(mask.sum()) if mask is not None else 0
    if M is None or n < MIN_INLIERS:
        raise NotMeasurable(f"only {n} inliers")
    roll, dx, dy, scale = decompose(M, ga.shape[1], ga.shape[0])
    return {"roll_deg": roll, "dx_px": dx / f, "dy_px": dy / f,
            "scale": scale, "inliers": n, "matches": len(matches)}


def fit_roll(wrists, roll_rates, k=1.0, k_grid=None):
    """Fit roll_rate(w) = g * sin(k * (w - w_level)).

    `roll_rates` are image-roll degrees per degree of base turn, one per wrist
    angle. For a fixed k the model is LINEAR in (A, B):
        g sin(k w - k w0) = A sin(k w) + B cos(k w),
        A = g cos(k w0),  B = -g sin(k w0),
    so it is solved exactly, and k (the wrist's own gain) is scanned over
    `k_grid` when given. g is returned positive; the sign convention of the
    roll is absorbed into w_level.

    Returns {g, w_level, k, rms, n}.
    """
    w = np.asarray(wrists, dtype=float)
    r = np.asarray(roll_rates, dtype=float)
    if len(w) < 4:
        raise NotMeasurable(f"need at least 4 tilts, have {len(w)}")
    if np.ptp(w) < 40.0:
        raise NotMeasurable(f"tilts span {np.ptp(w):.0f} deg; need 40+ to "
                            "see the sine bend")
    best = None
    for kk in (k_grid if k_grid is not None else [k]):
        x = np.radians(kk * w)
        X = np.column_stack([np.sin(x), np.cos(x)])
        (A, B), *_ = np.linalg.lstsq(X, r, rcond=None)
        rss = float(np.sum((X @ np.array([A, B]) - r) ** 2))
        if best is None or rss < best[0]:
            best = (rss, kk, A, B)
    rss, kk, A, B = best
    g = math.hypot(A, B)
    w0 = math.degrees(math.atan2(-B, A)) / kk
    # Of the two equivalent solutions (w0, w0 + 180/k with g flipped), keep
    # the level angle inside or nearest the range that was measured.
    period = 360.0 / kk
    lo, hi = float(w.min()), float(w.max())
    cands = [w0 + j * period / 2.0 for j in range(-4, 5)]
    w0 = min(cands, key=lambda c: 0.0 if lo <= c <= hi
             else min(abs(c - lo), abs(c - hi)))
    return {"g": round(g, 4), "w_level": round(w0, 1), "k": kk,
            "rms": round(math.sqrt(rss / len(w)), 4), "n": int(len(w))}


def elevation(wrist, fit):
    """Elevation above level of the view at this wrist angle, degrees.
    Signed the way the fit's roll was signed; use differences, or check one
    pose by eye, before trusting which of up/down is positive."""
    return fit["k"] * (float(wrist) - fit["w_level"])


def px_per_deg(wrists, slides, fit, min_cos=0.3):
    """Pixels per camera degree, from the slide against the roll fit.

    slide(w) = g cos(e(w)) * P for far scenery, and the roll fit already knows
    g and e(w), so P is a one-number least-squares fit. Near-level views carry
    it; views near straight up (cos e small) are left out, as their slide is
    mostly noise. Near scenery adds parallax and biases P -- the dome sweep
    showed most views from the pole shape are walls, i.e. far.

    Returns (P, rms) -- the horizontal FOV is frame_width / P.
    """
    c = np.array([fit["g"] * math.cos(math.radians(elevation(w, fit)))
                  for w in wrists])
    s = np.asarray(slides, dtype=float)
    keep = np.abs(c) >= min_cos * fit["g"]
    if keep.sum() < 2:
        raise NotMeasurable("fewer than 2 views near enough to level")
    c, s = c[keep], s[keep]
    P = float(np.sum(c * s) / np.sum(c * c))
    # Sign: the slide's direction convention is arbitrary; P is a magnitude.
    rms = float(np.sqrt(np.mean((c * P - s) ** 2)))
    return abs(P), rms


# A commanded turn at least this large must change the picture.
STUCK_MIN_TURN_DEG = 5.0


def stuck(reg, commanded_deg, px_per_deg=24.0):
    """Did the joint fail to move? From a registration between the picture
    before and after a commanded base turn.

    Found 2026-10-06: base_yaw stopped responding partway through a survey,
    silently -- the commands went out, nothing in the log -- and eight stops
    filed the same picture under eight different yaws. A real turn of 5+ deg
    slides or rolls the picture by tens of pixels at any tilt but the robot's
    own base (wrist ~44, never surveyed), so an unchanged picture with plenty
    of matched features means the joint did not move.

    `reg` is register()'s dict, or None when the view had too little texture
    to tell -- which is "unknown", never "stuck".
    """
    if reg is None or abs(commanded_deg) < STUCK_MIN_TURN_DEG:
        return False
    moved_px = math.hypot(reg["dx_px"], reg["dy_px"])
    # Roll of r degrees moves a point 300 px from the centre by ~5 px per deg.
    moved_px += abs(reg["roll_deg"]) * 300.0 * math.pi / 180.0
    return moved_px < 0.15 * abs(commanded_deg) * px_per_deg
