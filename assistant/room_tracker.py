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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--fps", type=float, default=2.0,
                    help="watch loop rate while idle (cheap, no model loaded)")
    ap.add_argument("--motion-fraction", type=float, default=0.02,
                    help="fraction of changed pixels that counts as motion")
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
    try:
        while True:
            cap = open_camera(args.camera)
            log(f"watching for motion, no model loaded "
                f"(threshold {args.motion_fraction}, {args.motion_frames} frames)")
            frac = watch_for_motion(cap, det, args)
            log(f"motion {frac:.4f} >= {args.motion_fraction} - handing over the camera")
            cap.release()
            # DirectShow needs a moment to let go before another process opens it.
            time.sleep(1.5)

            t0 = time.time()
            rc = run_logger(args)
            ran = time.time() - t0
            log(f"logger exited rc={rc} after {ran:.0f}s - GPU released")

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
