"""Which way does the picture move when a joint angle goes up?

    python direction_check.py

Needed because `look_at` is a feedback loop. It corrects yaw from a target's
offset in the image, so with the sign backwards it accelerates away from
whatever it is tracking instead of settling on it. Getting this from inference
rather than measurement has already gone wrong twice today:

  - Phase correlation returned peak responses of 0.00 to 0.07 on this scene --
    no peak at all -- and for a 15 degree pan it reported a 5.8 px shift and
    gave two opposite commands the SAME sign, which is impossible.
  - Reading a two-position wrist sweep by eye gave a direction that the taught
    poses later contradicted outright.

So this uses dense optical flow (Farneback), which gives a vector per pixel and
degrades into noise rather than into a confident wrong answer. The median of
that field is the scene's motion, and the median is used rather than the mean
so that a moving person or a flickering highlight cannot drag it.

Reading the result:
  the camera rides the arm, so the scene moves OPPOSITE to the camera.
  scene moves LEFT  (dx < 0) -> the camera panned RIGHT
  scene moves DOWN  (dy > 0) -> the camera tilted UP
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import settings                                              # noqa: E402
from observer import open_camera, read_fresh                 # noqa: E402

from avi_bot.motion import backends, poses, safety           # noqa: E402

W, H = 640, 360
MIN_PX = 4.0          # below this the flow is not distinguishable from noise

# A verdict is only given when the signal is at least this many times the
# spread between repeats. Without it this script did exactly what phase
# correlation did: printed a confident sign for base_yaw off a 2.6 px signal
# with 18.9 px of scatter between two runs.
MIN_SIGNAL_TO_SPREAD = 2.0

# Degrees to move each joint. Per joint, because yaw sweeps the whole frame:
# 10 degrees of yaw is about 107 px of a 640 px image, far enough that dense
# flow loses the correspondence entirely and returns noise. The wrist and
# shoulder move the view much less per degree, so they need a bigger swing to
# rise above that same noise.
SWING = {"base_yaw": 3.0, "shoulder": 10.0, "elbow": 10.0, "wrist": 10.0}


def log(msg):
    print(f"[dir {datetime.now():%H:%M:%S}] {msg}", flush=True)


def grey(cap):
    for _ in range(6):
        ok, frame = read_fresh(cap)
        if ok and frame is not None:
            return cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (W, H))
    raise RuntimeError("no frame")


def flow_shift(a, b):
    """Median (dx, dy) of the dense flow from a to b, in pixels."""
    f = cv2.calcOpticalFlowFarneback(a, b, None,
                                     pyr_scale=0.5, levels=4, winsize=25,
                                     iterations=3, poly_n=7, poly_sigma=1.5,
                                     flags=0)
    mag = np.linalg.norm(f, axis=2)
    # Flat wall contributes no real flow, only noise. Keep the pixels that
    # actually moved, or fall back to everything if nothing clearly did.
    strong = mag > max(0.5, float(np.percentile(mag, 70)))
    if strong.sum() < 500:
        strong = np.ones_like(mag, dtype=bool)
    return (float(np.median(f[..., 0][strong])),
            float(np.median(f[..., 1][strong])))


def hold(backend, limiter, target, seconds, hz=50.0):
    period = 1.0 / hz
    end = time.time() + seconds
    while time.time() < end:
        backend.send(limiter.apply(target, period), time.time())
        time.sleep(period)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--port-name", default="COM5")
    ap.add_argument("--swing", type=float, default=0.0,
                    help="override the per-joint swing in SWING")
    ap.add_argument("--repeats", type=int, default=2)
    args = ap.parse_args()

    config = poses.load()
    backend = backends.SerialBackend(args.port_name, config).open()
    st = backend.read_state()
    start = st[0] if st else poses.get_pose("HOME")
    log("arm reports " + " ".join(f"{j}={start[j]:.1f}" for j in poses.JOINTS))
    limiter = safety.Limiter(config, start=start)
    cap = open_camera(args.camera)
    log("camera open")

    # A mid-range pose, so every joint has room to move both ways and nothing
    # is measured while pressed against a soft limit.
    swing = {j: (args.swing or SWING.get(j, 10.0)) for j in poses.JOINTS}
    base = {}
    for j in poses.JOINTS:
        lo, hi = config.soft_limits(j)
        base[j] = min(max(90.0, lo + swing[j] + 2), hi - swing[j] - 2)
    hold(backend, limiter, base, 4.0)

    results = {}
    try:
        for j in poses.JOINTS:
            if not config.is_driven(j):
                continue
            samples = []
            for _ in range(args.repeats):
                hold(backend, limiter, base, 2.0)
                a = grey(cap)
                moved = dict(base)
                moved[j] = base[j] + swing[j]
                hold(backend, limiter, moved, 2.5)
                b = grey(cap)
                samples.append(flow_shift(a, b))
                hold(backend, limiter, base, 2.5)
            dx = float(np.median([s[0] for s in samples]))
            dy = float(np.median([s[1] for s in samples]))
            results[j] = (dx, dy, samples, swing[j])
            log(f"{j}: +{swing[j]:.0f} deg -> scene dx={dx:+.1f} "
                f"dy={dy:+.1f} px  (n={len(samples)})")
    finally:
        hold(backend, limiter, poses.get_pose("HOME"), 3.0)
        cap.release()
        backend.home()
        time.sleep(0.3)
        backend.close()

    print("\n" + "=" * 70, flush=True)
    print("a POSITIVE change in each joint angle moves the view:", flush=True)

    def call(axis_vals, signal):
        """A direction, or None when the repeats do not agree well enough."""
        spread = (max(axis_vals) - min(axis_vals)) if len(axis_vals) > 1 else 0.0
        if abs(signal) < MIN_PX:
            return None, spread, "below the noise floor"
        if spread > 0 and abs(signal) < MIN_SIGNAL_TO_SPREAD * spread:
            return None, spread, "repeats disagree"
        return signal, spread, ""

    verdicts = {}
    for j, (dx, dy, samples, sw) in results.items():
        sx, spread_x, why_x = call([s[0] for s in samples], dx)
        sy, spread_y, why_y = call([s[1] for s in samples], dy)
        parts = []
        if sx is not None:
            parts.append("RIGHT" if sx < 0 else "LEFT")
        if sy is not None:
            parts.append("UP" if sy > 0 else "DOWN")
        verdicts[j] = (sx, sy)
        said = " and ".join(parts) if parts else f"NO CALL ({why_x or why_y})"
        print(f"  {j:9} +{sw:4.1f} deg -> {said:26} "
              f"dx={dx:+6.1f}+-{spread_x:4.1f}  dy={dy:+6.1f}+-{spread_y:4.1f}",
              flush=True)

    print("", flush=True)
    sx = verdicts.get("base_yaw", (None, None))[0]
    if sx is None:
        print("base_yaw: NO CALL. Its sign stays unverified, and look_at must "
              "not ship on it -- a feedback loop with the sign backwards runs "
              "away from its target rather than settling.", flush=True)
    else:
        want = +1 if sx < 0 else -1
        have = config.sign("base_yaw")
        print(f"base_yaw: for 'larger degrees = look further right', "
              f"config/joints.toml wants sign = {want:+d}; it has {have:+d}. "
              + ("AGREES." if want == have else "DISAGREES -- change it."),
              flush=True)
    sy = verdicts.get("wrist", (None, None))[1]
    if sy is not None:
        print(f"wrist: larger degrees tilt the view "
              f"{'UP' if sy > 0 else 'DOWN'}. The taught poses say UP "
              f"(REST, camera down, is 30.5), so these "
              f"{'agree' if sy > 0 else 'DISAGREE'}.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
