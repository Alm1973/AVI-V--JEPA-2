"""Is it actually there? The search behind a look, without the hardware.

"look at the laptop" turned to the room map's aim and ended on a mug, with
nothing said (robot, 2026-10-08): the map's aims were 15+ degrees off. A look
now checks the picture at the aim, then at a few stops around it, and says
where -- if anywhere -- the thing turned up. The dashboard supplies moving
and detecting (dashboard/server.py confirm_look); this module only decides
where to look and what counts as found, so it can be tested on the Mac.
"""

# Mean frame level (0-255) below which a missed look proves nothing. At 9
# the detector found nothing at all; at 14 only a lit laptop screen
# (robot, 2026-10-08/09). Same line as /detect_debug's "dark". In the dark
# only the aim itself is checked, and a miss is "too dark", not "gone".
DARK_LEVEL = 30.0

# (base_yaw, wrist) offsets from the aim, degrees, in the order tried. On
# the robot the mug turned up at -15 yaw; the laptop, close and low, at
# none of the side stops until the arm was given time to arrive.
STOPS = ((0.0, 0.0), (-15.0, 0.0), (15.0, 0.0), (0.0, -12.0), (0.0, 12.0))


def best(kept, want):
    """The most confident detection labelled `want`, or None."""
    return max((d for d in kept if d["label"] == want),
               key=lambda d: d["conf"], default=None)


def search(want, start, move, detect, yaw_limits, wrist_limits,
           stops=STOPS):
    """Look for `want` at each stop around `start` = (yaw, wrist).

    move(yaw, wrist) puts the camera there (the caller skips what is already
    there and waits for the servos); detect() returns (detections, width,
    height) or None. Returns {"hit", "w", "h", "offset", "pose"} for the
    first stop that sees it, or None after the last.
    """
    for dyaw, dwr in stops:
        yaw = max(yaw_limits[0], min(yaw_limits[1], start[0] + dyaw))
        wrist = max(wrist_limits[0], min(wrist_limits[1], start[1] + dwr))
        move(yaw, wrist)
        got = detect()
        if got is None:
            continue
        kept, w, h = got
        hit = best(kept, want)
        if hit is not None:
            return {"hit": hit, "w": w, "h": h, "offset": (dyaw, dwr),
                    "pose": (yaw, wrist)}
    return None


# --- has the arm actually arrived? ------------------------------------
# A look's detections were read as if taken at the stop the arm was SENT
# to. Going down 15 degrees means undershooting ~20 for the backlash and
# coming back up, and 0.8 s did not always cover it: a frame from mid-swing,
# the mug far right of centre, was filed as seen from the stop -- and the
# map moved the mug 15 degrees the wrong way, twice (robot, 2026-10-08).
# Nothing reads the servos' real angles, so the picture is the evidence:
# when consecutive frames stop changing, the camera has stopped.

# Mean absolute difference between two 64x36 grey thumbnails (0-255) below
# which two frames count as the same view. Measured on the robot
# (assistant/still_probe.py, 2026-10-08 evening, stream ~7 fps): still
# 0.33; the frame a 15-degree turn happened in 5.86; then ~2 s of wobble at
# 0.5-1.8 as the arm settles. 1.0 waits out the move and most of the wobble
# without waiting for the last tremor, which costs a detector nothing.
STILL_DIFF = 1.0
THUMB = (36, 64)


def thumb(frame):
    """A frame as a small grey picture, by block averaging (no OpenCV)."""
    import numpy as np
    f = np.asarray(frame, dtype=np.float32)
    if f.ndim == 3:
        f = f.mean(axis=2)
    h, w = f.shape
    th, tw = THUMB
    f = f[:h - h % th, :w - w % tw]
    return f.reshape(th, f.shape[0] // th, tw, f.shape[1] // tw).mean(
        axis=(1, 3))


def wait_still(next_frame, timeout=2.5, clock=None, still=STILL_DIFF,
               calm=2):
    """Read frames until `calm` consecutive pairs differ by less than
    `still`, or `timeout` seconds pass. next_frame() returns a NEW frame
    each call (or None when there is no stream). True if it went still."""
    import time
    import numpy as np
    clock = clock or time.time
    t_end = clock() + timeout
    prev, quiet = None, 0
    while clock() < t_end:
        f = next_frame()
        if f is None:
            return False
        t = thumb(f)
        if prev is not None:
            quiet = quiet + 1 if float(np.abs(t - prev).mean()) < still \
                else 0
            if quiet >= calm:
                return True
        prev = t
    return False


def centred(det, w, h, frac=(0.15, 0.2)):
    """Is a detection near the middle of the picture? Only then is "the
    pose that centres it" a measurement of where it is."""
    return (abs(det["cx"] - w / 2.0) <= frac[0] * w
            and abs(det["cy"] - h / 2.0) <= frac[1] * h)
