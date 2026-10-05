"""Room tracker: keep the GPU free until the room is actually being used.

Motion on -> the logger runs. No motion -> the logger is not even loaded.

While the room is idle this process holds the camera and does nothing but a
160x90 grayscale frame difference, which costs almost no CPU and zero VRAM.
Once the room moves it hands the camera to observer.py, which loads
Qwen2.5-VL (~3.4 GB of the 6 GB card) and logs normally. When observer.py sees
nobody at the desk for --away-timeout it exits by itself, the VRAM is released,
and we go back to watching.

Only one process can hold the C270 through DirectShow, so the camera is owned by
exactly one of the two at any moment: released here before the logger starts,
re-acquired only after it has exited.

It reuses ChangeDetector's _prep/_fraction rather than reimplementing the frame
differencing, so the two agree on what counts as a changed pixel.
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime

import settings
from change import ChangeDetector
from observer import open_camera


def log(msg):
    print(f"[tracker {datetime.now():%H:%M:%S}] {msg}", flush=True)


def watch_for_motion(cap, det, args):
    """Hold the camera until motion is confirmed. Returns the motion fraction.

    Requires --motion-frames consecutive moving frames, so one noisy frame or a
    lighting flicker does not spin up a 3.4 GB model.
    """
    prev = None
    hits = 0
    period = 1.0 / args.fps
    # Report the quiet-room noise floor periodically. The right --motion-fraction
    # depends on the room and the lighting, and there is no way to guess it from
    # here, so make it measurable instead.
    seen_max = 0.0
    next_report = time.time() + args.report_every
    while True:
        t0 = time.time()
        ok, frame = cap.read()
        if ok and frame is not None:
            small = det._prep(frame)
            if prev is not None:
                frac = det._fraction(small, prev)
                seen_max = max(seen_max, frac)
                if frac >= args.motion_fraction:
                    hits += 1
                    if hits >= args.motion_frames:
                        return frac
                else:
                    hits = 0
            prev = small
        if args.report_every and time.time() >= next_report:
            log(f"idle: peak motion {seen_max:.5f} over the last "
                f"{args.report_every:.0f}s (threshold {args.motion_fraction})")
            seen_max = 0.0
            next_report = time.time() + args.report_every
        time.sleep(max(0.0, period - (time.time() - t0)))


def run_logger(args):
    cmd = [sys.executable, "-u", "observer.py", "--no-preview",
           "--exit-after-away", str(args.away_timeout)]
    if args.audit_rate is not None:
        cmd += ["--audit-rate", str(args.audit_rate)]
    log("starting logger: " + " ".join(cmd[1:]))
    return subprocess.call(cmd)


# Seconds to wait after one process releases the camera before another opens
# it. DirectShow hands the device over lazily.
HANDOFF_S = 2.5

# Consecutive failed cycles before the tracker stops trying. High enough to ride
# out a camera that is slow to come back, low enough that a genuinely broken
# camera does not spin all night.
MAX_CONSECUTIVE_FAILS = 5


def acquire_camera(args, attempts=6):
    """open_camera, with backoff.

    The camera is passed back and forth between this process and the logger, and
    DirectShow does not always release it promptly. After a 20-minute logger
    run, open_camera's 8-second blank-frame budget was not enough, and a single
    miss used to kill the tracker outright.
    """
    delay = 2.0
    for i in range(1, attempts + 1):
        try:
            return open_camera(args.camera)
        except RuntimeError as exc:
            if i == attempts:
                raise
            log(f"camera not ready ({exc}) - attempt {i}/{attempts}, "
                f"waiting {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2.0, 30.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--fps", type=float, default=2.0,
                    help="watch loop rate while idle (cheap, no model loaded)")
    ap.add_argument("--motion-fraction", type=float, default=0.005,
                    help="fraction of changed pixels that counts as motion. 0.005 is from "
                         "measurement: a still room reads 0.00000 and a hand at the lens "
                         "reads 0.67-0.83. Provisional - re-check after the camera is re-aimed")
    ap.add_argument("--motion-frames", type=int, default=3,
                    help="consecutive moving frames needed before starting the logger")
    ap.add_argument("--away-timeout", type=float, default=600.0,
                    help="logger exits after this long with nobody at the desk")
    ap.add_argument("--audit-rate", type=float, default=None,
                    help="passed through to observer.py")
    ap.add_argument("--report-every", type=float, default=300.0, metavar="SECONDS",
                    help="log the peak idle motion this often, to calibrate "
                         "--motion-fraction for the room. 0 disables")
    ap.add_argument("--cycles", type=int, default=0,
                    help="stop after this many motion->log->idle cycles (0 = forever)")
    args = ap.parse_args()

    det = ChangeDetector()
    cycles = 0
    fails = 0
    try:
        while True:
            try:
                cap = acquire_camera(args)
                log(f"watching for motion, no model loaded "
                    f"(threshold {args.motion_fraction}, {args.motion_frames} frames)")
                frac = watch_for_motion(cap, det, args)
                log(f"motion {frac:.4f} >= {args.motion_fraction} - handing over the camera")
                cap.release()
                # DirectShow needs a moment to let go before another process opens it.
                time.sleep(HANDOFF_S)

                t0 = time.time()
                rc = run_logger(args)
                ran = time.time() - t0
                log(f"logger exited rc={rc} after {ran:.0f}s - GPU released")
                # And the same courtesy in the other direction. The logger's
                # own process has to die before DirectShow frees the device.
                time.sleep(HANDOFF_S)
                fails = 0
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # This runs unattended for days. A transient fault must cost one
                # cycle, not the whole run -- which is exactly what happened on
                # 2026-10-04: the camera was not ready when the logger handed it
                # back, open_camera raised, and the process exited rc=1 at 23:23
                # having logged for 20 minutes out of a planned evening.
                fails += 1
                if fails >= MAX_CONSECUTIVE_FAILS:
                    log(f"{fails} consecutive failures, giving up: {exc!r}")
                    raise
                back = min(30.0 * fails, 300.0)
                log(f"cycle failed ({exc!r}) - failure {fails}/"
                    f"{MAX_CONSECUTIVE_FAILS}, retrying in {back:.0f}s")
                time.sleep(back)
                continue

            cycles += 1
            if args.cycles and cycles >= args.cycles:
                log(f"reached --cycles {args.cycles}, stopping")
                return
            if ran < 30:
                # Something is wrong (bad camera handoff, model failure). Do not
                # hot-loop a 3.4 GB load.
                log("logger exited suspiciously fast - backing off 30s")
                time.sleep(30)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
