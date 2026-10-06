"""Run every gesture on the real arm and measure what actually happened.

    python gesture_check.py              # all of them
    python gesture_check.py --only nod shake

This is M1's "every gesture runs" check, done on hardware instead of in sim. It
owns the camera and the serial port directly rather than going through teleop,
because teleop's driver continuously rewrites the base pose from its own held-key
state and would fight a gesture for control of it.

TWO NUMBERS PER GESTURE, and the pair is the point:

  commanded   the largest angle any joint was actually told to move, taken from
              the limiter's output, so it is what went down the wire.
  observed    how much the picture changed, since the camera rides the arm.

Together they separate three failures that look identical in a log:
  commanded small                -> the gesture is being scaled down (the arm's
                                    measured 28 deg/s ceiling does this)
  commanded large, observed none -> the servo is not following: stall, power,
                                    or a wrong direction driving into a stop
  both healthy                   -> it worked

Frames are compared in memory and never written to disk.
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

from avi_bot.motion import backends, engine, gestures, poses  # noqa: E402

STILL = 3.0          # mean abs grey-level difference that counts as motion


def log(msg):
    print(f"[gcheck {datetime.now():%H:%M:%S}] {msg}", flush=True)


def small(frame):
    return cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (320, 180))


def settle(eng, cap, seconds, now):
    """Hold the current target and let the arm and the picture stop."""
    step = 1.0 / eng.config.tick_hz
    end = now + seconds
    while now < end:
        eng.tick(now)
        read_fresh(cap)
        now += step
        time.sleep(step)
    return now


def run_one(name, eng, cap, now, hold=1.5):
    cfg = eng.config
    step = 1.0 / cfg.tick_hz
    start_pose = poses.get_pose(gestures.START_POSE.get(name, "HOME"))

    eng.base = dict(start_pose)
    eng.limiter.reset(eng.last_sent)
    now = settle(eng, cap, 2.5, now)

    prev = small(read_fresh(cap)[1])
    base0 = dict(eng.last_sent)

    move = gestures.make(name)
    eng.push(move)
    eng.tick(now)
    now += step

    peak_obs = 0.0
    excursion = {j: 0.0 for j in poses.JOINTS}
    t0 = now
    deadline = now + max(8.0, move.duration + 4.0)
    while now < deadline:
        eng.tick(now)
        for j in poses.JOINTS:
            excursion[j] = max(excursion[j],
                               abs(eng.last_sent[j] - base0[j]))
        ok, frame = read_fresh(cap)
        if ok and frame is not None:
            cur = small(frame)
            peak_obs = max(peak_obs, float(cv2.absdiff(prev, cur).mean()))
            prev = cur
        now += step
        time.sleep(step)
        if not eng.busy() and now - t0 > move.duration + hold:
            break

    commanded = max(excursion.values())
    worst = max(excursion, key=excursion.get)
    if commanded < 0.5:
        verdict = "COMMANDED NOTHING"
    elif peak_obs > STILL:
        verdict = "ok"
    else:
        verdict = "NOT FOLLOWING"
    return now, {
        "name": name, "commanded": commanded, "joint": worst,
        "observed": peak_obs, "duration": move.duration, "verdict": verdict,
        "excursion": excursion,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--port-name", default="COM5")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--sim", action="store_true",
                    help="no serial port; proves the harness, not the arm")
    args = ap.parse_args()

    config = poses.load()
    if args.sim:
        backend = backends.SimBackend(config)
        start = poses.get_pose("HOME")
    else:
        backend = backends.SerialBackend(args.port_name, config).open()
        st = backend.read_state()
        start = st[0] if st else poses.get_pose("HOME")
        log("arm reports " +
            " ".join(f"{j}={start[j]:.1f}" for j in poses.JOINTS))

    eng = engine.MotionEngine(config, backend, start_pose=start)
    # Breathing and saccades would add motion of their own and make every
    # gesture look like it worked.
    for layer in eng.layers:
        layer.weight = 0.0

    cap = open_camera(args.camera)
    log("camera open")
    log(f"arm can do {config.achievable:.0f} deg/s (measured); gestures are "
        f"sized to fit that")

    names = args.only or sorted(gestures.GESTURES)
    bad = [n for n in names if n not in gestures.GESTURES]
    if bad:
        log(f"unknown gesture(s): {bad}")
        return 2

    rows = []
    now = 0.0
    try:
        for name in names:
            now, r = run_one(name, eng, cap, now)
            rows.append(r)
            log(f"{name:16} commanded {r['commanded']:5.1f} deg "
                f"({r['joint']})  observed {r['observed']:5.1f}  "
                f"{r['verdict']}")
    finally:
        eng.clear()
        eng.base = poses.get_pose("HOME")
        settle(eng, cap, 2.5, now)
        cap.release()
        backend.home()
        time.sleep(0.3)
        backend.close()

    print("\n" + "=" * 72, flush=True)
    print(f"{'gesture':16} {'dur':>5} {'commanded':>10} {'joint':<9} "
          f"{'observed':>8}  verdict", flush=True)
    for r in rows:
        print(f"{r['name']:16} {r['duration']:5.2f} {r['commanded']:10.1f} "
              f"{r['joint']:<9} {r['observed']:8.1f}  {r['verdict']}",
              flush=True)
    okc = sum(r["verdict"] == "ok" for r in rows)
    print(f"\n{okc}/{len(rows)} moved the arm and the picture.", flush=True)
    for r in rows:
        if r["verdict"] != "ok":
            print(f"  {r['name']}: {r['verdict']} -- " +
                  " ".join(f"{j}={r['excursion'][j]:.1f}"
                           for j in poses.JOINTS), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
