"""Observation logger: milestone 4 of the desk assistant.

Watches the desk with the C270, and on a scene change (OpenCV) or a heartbeat
asks the VLM what you're doing and which desk objects are really there. Writes
the answers to SQLite. Frames are never saved, except audit thumbnails if you
turn them on.

Run from inside this folder:
    python observer.py                  # with a preview window
    python observer.py --no-preview     # headless
    python observer.py --audit-rate 0.05

Preview keys: p = pause/resume, g = guest mode on/off, q = quit.
Creating a file named PAUSE in this folder also pauses it.
"""
import argparse
import os
import random
import sys
import time
from datetime import datetime

import cv2

import settings
from change import ChangeDetector


def padded_crop(frame, box, pad):
    x1, y1, x2, y2 = box
    h, w = frame.shape[:2]
    bw, bh = x2 - x1, y2 - y1
    cx1 = max(0, int(x1 - pad * bw))
    cy1 = max(0, int(y1 - pad * bh))
    cx2 = min(w, int(x2 + pad * bw))
    cy2 = min(h, int(y2 + pad * bh))
    return frame[cy1:cy2, cx1:cx2]


class Observer:
    def __init__(self, vlm, detector, memory, change=None, rng=None,
                 clock=time.time, audit_rate=settings.AUDIT_RATE,
                 audit_dir=settings.AUDIT_DIR, mc_passes=settings.MC_PASSES,
                 pause_file=settings.PAUSE_FILE, pose_fn=None):
        self.vlm = vlm
        self.detector = detector
        self.memory = memory
        self.change = change or ChangeDetector()
        self.rng = rng or random.Random()
        self.clock = clock
        self.audit_rate = audit_rate
        self.audit_dir = audit_dir
        self.mc_passes = mc_passes
        self.pause_file = pause_file
        # Returns the arm's joints, or None. The observer has no arm of its
        # own and must not acquire one -- it runs standalone as often as it
        # runs under the dashboard. When nothing supplies a pose the sighting
        # records NULL angles, and find() then declines to turn the arm rather
        # than assuming the camera was where it used to be.
        self.pose_fn = pose_fn
        self.paused = False
        self.guest = False
        self.last_obs_ts = None
        self.last_change_obs_ts = float("-inf")
        self.pending_change = None
        self.last_activity = None
        self.last_summary = ""

    def current_pose(self):
        """The arm's joints now, or None. Never raises.

        A pose source that throws must not lose the observation: the sighting
        is still true and still worth storing, it simply has no direction
        attached. Failing the whole write would trade a complete record for
        nothing at all.
        """
        if self.pose_fn is None:
            return None
        try:
            return self.pose_fn()
        except Exception:
            return None

    # --- control -------------------------------------------------------
    def is_paused(self):
        return self.paused or self.guest or (
            self.pause_file is not None and os.path.exists(self.pause_file))

    def heartbeat_s(self):
        if self.last_activity == settings.NOBODY:
            return settings.AWAY_HEARTBEAT_S
        return settings.HEARTBEAT_S

    # --- main step -----------------------------------------------------
    def step(self, frame, now):
        """Feed one frame. Returns the new observation id, or None."""
        if self.is_paused():
            # Keep the change detector's reference current, so resuming
            # doesn't fire on everything that happened while paused.
            self.change.reset(frame)
            return None

        changed = self.change.update(frame, now)
        if changed:
            self.pending_change = self.change.last_reason

        trigger = None
        if self.last_obs_ts is None:
            trigger = "startup"
        elif (self.pending_change
              and now - self.last_change_obs_ts >= settings.CHANGE_COOLDOWN_S):
            trigger = self.pending_change
        elif now - self.last_obs_ts >= self.heartbeat_s():
            trigger = "heartbeat"

        if trigger is None:
            return None
        return self.observe(frame, now, trigger)

    def observe(self, frame, now, trigger):
        t0 = self.clock()
        calls0 = self.vlm.calls

        raw, probs = self.vlm.choose(settings.ACTIVITY_QUESTION,
                                     settings.ACTIVITIES, frame,
                                     passes=self.mc_passes, rng=self.rng)
        p = probs[raw]
        activity = raw if p >= settings.ACTIVITY_MIN_P else settings.UNCERTAIN

        candidates, person_conf = self.detector.detect(frame)
        fh, fw = frame.shape[:2]
        sightings = []
        for c in candidates[:settings.MAX_VERIFY]:
            crop = padded_crop(frame, c["box"], settings.VERIFY_PAD)
            if crop.size == 0:
                continue
            _, margin = self.vlm.yes_no(
                settings.VERIFY_PROMPT.format(label=c["label"]), crop)
            x1, y1, x2, y2 = c["box"]
            sightings.append({
                "label": c["label"], "det_conf": c["conf"],
                "vlm_margin": round(float(margin), 3),
                "x": x1 / fw, "y": y1 / fh, "w": (x2 - x1) / fw, "h": (y2 - y1) / fh,
            })

        latency_ms = (self.clock() - t0) * 1000
        obs_id = self.memory.add_observation(
            ts=now, trigger=trigger, activity=activity, activity_raw=raw,
            activity_p=round(p, 4), probs=probs,
            person_conf=round(person_conf, 3),
            vlm_calls=self.vlm.calls - calls0, latency_ms=round(latency_ms, 1),
            sightings=sightings, pose=self.current_pose())

        if self.audit_rate > 0 and self.rng.random() < self.audit_rate:
            self.memory.set_audit_path(obs_id, self.save_thumbnail(frame, obs_id))

        self.last_obs_ts = now
        if trigger.startswith("change"):
            self.last_change_obs_ts = now
        self.pending_change = None
        if activity != settings.UNCERTAIN:
            self.last_activity = activity
        self.change.reset(frame)

        seen = [s for s in sightings if s["vlm_margin"] > 0]
        stamp = datetime.fromtimestamp(now).strftime("%H:%M:%S")
        objs = ", ".join(f"{s['label']}" for s in seen) or "no verified objects"
        self.last_summary = (f"[{stamp}] {trigger:<13} {activity} ({p:.2f}) | "
                             f"{objs} | {latency_ms:.0f} ms, "
                             f"{self.vlm.calls - calls0} VLM calls")
        return obs_id

    def save_thumbnail(self, frame, obs_id):
        os.makedirs(self.audit_dir, exist_ok=True)
        h, w = frame.shape[:2]
        scale = settings.AUDIT_WIDTH / w
        thumb = cv2.resize(frame, (settings.AUDIT_WIDTH, int(h * scale)),
                           interpolation=cv2.INTER_AREA)
        path = os.path.join(self.audit_dir, f"obs_{obs_id:06d}.jpg")
        cv2.imwrite(path, thumb)
        return path


# --- hardware loop ------------------------------------------------------
def open_camera(index):
    backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, settings.FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, settings.FRAME_HEIGHT)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    if not cap.isOpened():
        raise RuntimeError(f"camera {index} failed to open")
    t0 = time.time()
    while time.time() - t0 < 8.0:  # same warm-up idea as avi/camera.py
        ok, frame = cap.read()
        if ok and frame is not None and frame.mean() >= 1.0:
            return cap
        time.sleep(0.15)
    raise RuntimeError("camera still returns blank frames after 8 s")


def read_fresh(cap, drain=2):
    for _ in range(drain):
        cap.grab()
    return cap.read()


def draw_status(frame, obs):
    view = cv2.resize(frame, (640, 360))
    if obs.guest:
        status, color = "GUEST MODE - not logging", (0, 165, 255)
    elif obs.is_paused():
        status, color = "PAUSED - not logging", (0, 0, 255)
    else:
        status, color = "LOGGING", (0, 200, 0)
    cv2.putText(view, status, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    if obs.last_summary:
        cv2.putText(view, obs.last_summary[:80], (10, 350),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
    return view


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--db", default=settings.DB_PATH)
    ap.add_argument("--audit-rate", type=float, default=settings.AUDIT_RATE)
    ap.add_argument("--mc-passes", type=int, default=settings.MC_PASSES)
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--once", action="store_true",
                    help="make one observation, print it, and exit (for remote testing)")
    ap.add_argument("--exit-after-away", type=float, default=0.0, metavar="SECONDS",
                    help="exit once nobody has been at the desk this long, so the "
                         "GPU is freed. 0 disables. Used by room_tracker.py")
    args = ap.parse_args()
    if args.once:
        args.no_preview = True   # nobody is at the desk to see a window

    from detector import DeskDetector
    from memory import Memory
    from vlm import DeskVLM

    print("[observer] loading Qwen2.5-VL (4-bit) ...")
    vlm = DeskVLM()
    print("[observer] loading YOLO-World ...")
    detector = DeskDetector()
    memory = Memory(args.db)
    cap = open_camera(args.camera)
    obs = Observer(vlm, detector, memory, audit_rate=args.audit_rate,
                   mc_passes=args.mc_passes)
    print("[observer] running. Ctrl+C or q to stop.")

    period = 1.0 / settings.LOOP_FPS
    today = datetime.now().date().isoformat()
    once_deadline = time.time() + 60.0 if args.once else None
    once_t0 = time.time()
    last_present = time.time()   # for --exit-after-away
    try:
        while True:
            t_loop = time.time()
            ok, frame = read_fresh(cap)
            if not ok or frame is None:
                time.sleep(0.5)
                continue
            now = time.time()
            if obs.step(frame, now) is not None:
                print(obs.last_summary)
                if obs.last_activity != settings.NOBODY:
                    last_present = now
                if args.once:
                    print(f"[observer] --once: one observation in "
                          f"{time.time() - once_t0:.2f}s (includes camera warm-up)")
                    break

            if args.exit_after_away > 0 and (now - last_present) >= args.exit_after_away:
                print(f"[observer] nobody at the desk for {args.exit_after_away:.0f}s "
                      f"- exiting so the GPU is free")
                break

            if once_deadline is not None and time.time() > once_deadline:
                print("[observer] --once: no observation within 60s", file=sys.stderr)
                raise SystemExit(1)

            day = datetime.now().date().isoformat()
            if day != today:  # close out yesterday's timeline at midnight
                memory.rebuild_sessions(today)
                today = day

            if not args.no_preview:
                cv2.imshow("AVI observer", draw_status(frame, obs))
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("p"):
                    obs.paused = not obs.paused
                    print(f"[observer] {'paused' if obs.paused else 'resumed'}")
                if key == ord("g"):
                    obs.guest = not obs.guest
                    print(f"[observer] guest mode {'on' if obs.guest else 'off'}")
            time.sleep(max(0.0, period - (time.time() - t_loop)))
    except KeyboardInterrupt:
        pass
    finally:
        memory.rebuild_sessions(today)
        cap.release()
        cv2.destroyAllWindows()
        memory.close()
        print("[observer] stopped; today's sessions saved.")


if __name__ == "__main__":
    main()
