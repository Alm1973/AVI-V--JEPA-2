"""Is it actually there? The search behind a look, without the hardware.

"look at the laptop" turned to the room map's aim and ended on a mug, with
nothing said (robot, 2026-10-08): the map's aims were 15+ degrees off. A look
now checks the picture at the aim, then at a few stops around it, and says
where -- if anywhere -- the thing turned up. The dashboard supplies moving
and detecting (dashboard/server.py confirm_look); this module only decides
where to look and what counts as found, so it can be tested on the Mac.
"""

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
