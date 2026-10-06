"""Settle base_yaw's direction by tracking a person, then prove the loop.

    python yaw_sign_check.py

Four attempts to get this from scene features all failed -- see
docs/remote-notes.md. A person is a different kind of target: the detector
returns one box with a position, so there is nothing to average and no
correspondence to lose across a large shift.

TWO PARTS, and the second is the one that matters.

  1. OPEN LOOP. Move yaw a known amount and see which way the person's box
     moves. UNRELIABLE IN PRACTICE, and kept only as a cross-check: measured
     twice, it reported 0.8 % and 2.9 % of a frame for an 8 degree move that
     should give 28 %, almost certainly because a person sitting at a desk
     shifts position over the three seconds it takes. A single snapshot of a
     moving target is the wrong instrument.

  2. CLOSED LOOP. Run the actual look_at correction with the sign currently in
     config, and watch whether the error shrinks or grows. This cannot be
     confused by which frame of reference "right" means, because it tests the
     only property that matters: does the loop settle, or run away.

Needs Shaurya in frame. Nothing is written to disk.
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import settings                                              # noqa: E402
from observer import open_camera, read_fresh                 # noqa: E402

from avi_bot.motion import backends, poses, safety           # noqa: E402

# MEASURED, not from the datasheet: about 29 COMMANDED degrees of base yaw move
# the image by one frame width.
#
#   a 7.4 deg correction moved the person 0.248 of a frame -> 29.8 deg/frame
#   an 8.8 deg correction moved them 0.323 of a frame      -> 27.2 deg/frame
#
# The C270 is specified at 60 degrees diagonal, which is about 52 horizontal
# for a 16:9 frame, so this is roughly half what the datasheet implies. The
# gap is real but its cause is CONFOUNDED: it is either a narrower effective
# field of view or the yaw under-travelling what it is commanded, and these
# measurements cannot separate them.
#
# For a feedback loop that does not matter, which is why this is the right
# number to keep. look_at outputs commanded degrees and observes frame
# fractions, so a constant calibrated in exactly those units is what it needs;
# separating the two causes would change nothing about the correction. Using 60
# here made the loop apply roughly twice the intended gain, and it oscillated
# +0.247 / -0.293 / +0.030 instead of settling.
DEG_PER_FRAME = 29.0


def log(msg):
    print(f"[yaw {datetime.now():%H:%M:%S}] {msg}", flush=True)


def hold(backend, limiter, target, seconds, hz=50.0):
    period = 1.0 / hz
    end = time.time() + seconds
    while time.time() < end:
        backend.send(limiter.apply(target, period), time.time())
        time.sleep(period)


def find_person(cap, det, tries=8):
    """Median of a few detections, so one bad frame cannot decide anything."""
    seen = []
    for _ in range(tries):
        ok, frame = read_fresh(cap)
        if not ok or frame is None:
            continue
        p = det.person(frame)
        if p:
            seen.append((p["cx"], frame.shape[1], p["conf"]))
    if not seen:
        return None
    seen.sort()
    cx, width, conf = seen[len(seen) // 2]
    return {"cx": cx, "width": width, "conf": conf,
            "offset_frac": (cx - width / 2.0) / width,
            "n": len(seen)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--port-name", default="COM5")
    ap.add_argument("--swing", type=float, default=8.0)
    ap.add_argument("--gain", type=float, default=0.5,
                    help="fraction of the measured error corrected per step")
    ap.add_argument("--steps", type=int, default=6)
    ap.add_argument("--pose", default="ATTENTIVE",
                    help="pose to start from. ATTENTIVE is the one BUILD_SPEC "
                         "defines as aimed at where he sits; OBSERVE is aimed "
                         "at the desk surface and may not see him at all.")
    args = ap.parse_args()

    config = poses.load()
    from detector import DeskDetector
    log("loading YOLO-World ...")
    det = DeskDetector()
    backend = backends.SerialBackend(args.port_name, config).open()
    st = backend.read_state()
    start = st[0] if st else poses.get_pose("OBSERVE")
    limiter = safety.Limiter(config, start=start)
    cap = open_camera(args.camera)
    log("camera open")

    pose = poses.get_pose(args.pose)
    hold(backend, limiter, pose, 3.5)

    a = find_person(cap, det)
    if not a:
        # Not an error worth giving up on: the taught poses aim at different
        # things, and OBSERVE in particular is aimed at the desk SURFACE, so it
        # can easily have nobody in it while he is sitting right there. Look
        # around before concluding he is absent.
        log(f"nobody visible from {args.pose}; searching the other poses ...")
        for name in ("ATTENTIVE", "TALL", "OBSERVE", "HOME"):
            if name == args.pose:
                continue
            cand = poses.get_pose(name)
            hold(backend, limiter, cand, 3.0)
            a = find_person(cap, det)
            log(f"  {name}: " + (f"found, conf {a['conf']:.2f}" if a
                                 else "nobody"))
            if a:
                pose = cand
                log(f"using {name} as the starting pose")
                break
    if not a:
        log("NO PERSON FOUND from any taught pose. Either sit where the "
            "camera can see you, or the camera needs re-aiming: run "
            "`./runHome teach` and look at the live feed.")
        cap.release(); backend.close()
        return 1
    log(f"person at x={a['cx']:.0f}/{a['width']} "
        f"(offset {a['offset_frac']:+.3f} of frame, conf {a['conf']:.2f})")

    # --- part 1: open loop ------------------------------------------------
    moved = dict(pose)
    moved["base_yaw"] = pose["base_yaw"] + args.swing
    hold(backend, limiter, moved, 3.0)
    b = find_person(cap, det)
    hold(backend, limiter, pose, 3.0)
    if not b:
        log("person lost after the move; try a smaller --swing")
        cap.release(); backend.close()
        return 1

    dx_frac = b["offset_frac"] - a["offset_frac"]
    expected = args.swing / DEG_PER_FRAME
    log(f"after +{args.swing:.0f} deg yaw, the person moved "
        f"{dx_frac:+.3f} of a frame (expected about "
        f"{expected:.3f} in magnitude)")
    if abs(dx_frac) < 0.25 * expected:
        log("the box barely moved; cannot call it")
        open_verdict = None
    else:
        open_verdict = -1 if dx_frac < 0 else +1
        log(f"box moved {'LEFT' if dx_frac < 0 else 'RIGHT'}, so the camera "
            f"panned {'RIGHT' if dx_frac < 0 else 'LEFT'}")

    # --- part 2: closed loop ---------------------------------------------
    # Steer to centre the person using the sign config already has. If that
    # sign is wrong the error grows instead of shrinking, which is the whole
    # failure mode look_at has to be protected from.
    log(f"closed loop with config's sign ({config.sign('base_yaw'):+d}) ...")
    errs = []
    cur = dict(pose)
    for i in range(args.steps):
        p = find_person(cap, det)
        if not p:
            log("person lost mid-loop")
            break
        errs.append(p["offset_frac"])
        correction = -p["offset_frac"] * DEG_PER_FRAME * args.gain
        lo, hi = config.soft_limits("base_yaw")
        cur["base_yaw"] = max(lo, min(hi, cur["base_yaw"] + correction))
        log(f"  step {i+1}: offset {p['offset_frac']:+.3f} -> "
            f"yaw {cur['base_yaw']:.1f} (correction {correction:+.1f} deg)")
        hold(backend, limiter, cur, 2.2)

    hold(backend, limiter, pose, 2.5)
    cap.release()
    backend.home()
    time.sleep(0.3)
    backend.close()

    print("\n" + "=" * 70, flush=True)
    # A verdict needs a real error to correct. Sitting centred gives a run of
    # near-zero offsets whose RATIO is meaningless noise -- 0.026 against 0.020
    # is a 1.3x "growth" and nothing at all. That is exactly what the second
    # run of this script reported as DIVERGED while the loop was in fact just
    # sitting still and working.
    MIN_START = 0.12
    if len(errs) >= 3 and abs(errs[0]) < MIN_START:
        print(f"INCONCLUSIVE: started {abs(errs[0]):.3f} off centre, which is "
              f"below the {MIN_START:.2f} needed to tell convergence from "
              f"detector jitter.", flush=True)
        print("  " + " ".join(f"{e:+.3f}" for e in errs), flush=True)
        print("Sit well off to one side of the frame and re-run.", flush=True)
    elif len(errs) >= 3:
        first, last = abs(errs[0]), abs(errs[-1])
        shrank = last < first * 0.7
        grew = last > first * 1.3
        print(f"closed loop: |offset| went {first:.3f} -> {last:.3f}",
              flush=True)
        print("  " + " ".join(f"{e:+.3f}" for e in errs), flush=True)
        if shrank:
            print(f"CONVERGED. config's sign "
                  f"({config.sign('base_yaw'):+d}) is CORRECT, and look_at "
                  f"can ship on it.", flush=True)
        elif grew:
            print(f"DIVERGED. config's sign ({config.sign('base_yaw'):+d}) is "
                  f"BACKWARDS -- flip base_yaw's sign in config/joints.toml.",
                  flush=True)
        else:
            print("neither clearly: try a larger --gain or sit further "
                  "off-centre to start.", flush=True)
    else:
        print("not enough closed-loop samples to judge.", flush=True)
    if open_verdict is not None:
        print(f"\nopen loop said: larger base_yaw degrees move the view "
              f"{'RIGHT' if open_verdict < 0 else 'LEFT'}.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
