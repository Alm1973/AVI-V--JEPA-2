"""AVI's always-on senses: camera -> MediaPipe -> text events. REDESIGN step 1.

    python -u senses.py --seconds 60          # a short test run, then a summary
    python -u senses.py                       # until stopped (run as a task)

Hands (Gesture Recognizer) at up to 15 Hz, body (Pose Landmarker lite) and face
(Face Landmarker + blendshapes + head pose) at 5 Hz, brightness every frame,
all on the CPU at 640x480 -- measured on the MSI 2026-10-12: 3 % of the
machine, camera-bound at ~15 fps (docs/REDESIGN.md section 11).

Events print as one line each, which is the text the brain will read later:
    [senses 16:02:11] arrived
    [senses 16:31:40] gesture {"name": "Thumb_Up", "hand": "Right", "facing_avi": true}

PRIVACY: frames never leave memory. What is stored is events, a 10-second time
series, and numbers MediaPipe derived (hand points, expression weights), in
senses-dev.db beside this file unless --profile live.

Runs in the Python 3.12 env (avi-desk\\.conda312), NOT .venv-yolo. Takes the
camera, so the dashboard must be stopped first (./runHome stop).
"""
import argparse
import json
import math
import pathlib
import sys
import time
import urllib.request
from datetime import datetime

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))           # avi\ -> import avi_bot
from avi_bot.senses import events as E          # noqa: E402
from avi_bot.senses.store import PROFILES, Store  # noqa: E402

MODELS = HERE / "models" / "mediapipe"
URLS = {
    "gesture_recognizer.task": "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task",
    "pose_landmarker_lite.task": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
    "face_landmarker.task": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task",
}
RATES = {"hands": 15.0, "body": 5.0, "face": 5.0}
VECTOR_EVERY = {"hand": 0.5, "face_blend": 1.0, "pose": 1.0}   # seconds
LYING_DEG = 55.0           # torso this far from vertical in the image = lying
VIS = 0.5                  # landmark visibility needed to use it
SAVE_LEARNED_S = 60.0


def log(msg):
    print(f"[senses {datetime.now():%H:%M:%S}] {msg}", flush=True)


def model(name):
    path = MODELS / name
    if not path.is_file():
        MODELS.mkdir(parents=True, exist_ok=True)
        log(f"downloading {name}")
        urllib.request.urlretrieve(URLS[name], path)
    return BaseOptions(model_asset_path=str(path))


def open_camera(index, size):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
    for _ in range(60):          # the C270's first ~5 frames are black with ok=True
        ok, frame = cap.read()
        if ok and frame.mean() >= 1.0:
            return cap
    cap.release()
    return None


# --- MediaPipe result -> plain values -----------------------------------------
def box_of(points):
    xs = [min(1.0, max(0.0, p.x)) for p in points]
    ys = [min(1.0, max(0.0, p.y)) for p in points]
    if not xs:
        return None
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return ((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0)


def body_of(landmarks, aspect):
    seen = [p for p in landmarks if getattr(p, "visibility", 1.0) >= VIS]
    box = box_of(seen)
    if box is None:
        return None
    sh, hp = (landmarks[11], landmarks[12]), (landmarks[23], landmarks[24])
    lying = None
    if all(p.visibility >= VIS for p in sh + hp):
        dx = ((hp[0].x + hp[1].x) - (sh[0].x + sh[1].x)) / 2 * aspect
        dy = ((hp[0].y + hp[1].y) - (sh[0].y + sh[1].y)) / 2
        lying = math.degrees(math.atan2(abs(dx), abs(dy))) > LYING_DEG
    return E.Body(box, lying)


def head_pose(matrix):
    """(yaw, pitch) in degrees from a face transformation matrix. 0,0 faces the camera."""
    r = matrix[:3, :3]
    yaw = math.degrees(math.atan2(r[0][2], r[2][2]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, -r[1][2]))))
    return yaw, pitch


def hand_vector(points):
    """21 points relative to the wrist, scaled to unit size: position- and
    distance-free, so the same hand shape gives the same vector anywhere."""
    w = points[0]
    rel = [(p.x - w.x, p.y - w.y, p.z - w.z) for p in points]
    scale = max(math.sqrt(x * x + y * y + z * z) for x, y, z in rel) or 1.0
    return [v / scale for xyz in rel for v in xyz]


class Eyes:
    def __init__(self):
        video = vision.RunningMode.VIDEO
        self.hands = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
            base_options=model("gesture_recognizer.task"), running_mode=video, num_hands=2))
        self.body = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
            base_options=model("pose_landmarker_lite.task"), running_mode=video, num_poses=2))
        self.face = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=model("face_landmarker.task"), running_mode=video, num_faces=3,
            output_face_blendshapes=True, output_facial_transformation_matrixes=True))


def report(store, hours):
    since = time.time() - hours * 3600
    evs = store.events(since=since)
    print(f"{store.counts(since=since)} in the last {hours:g} h")
    for ts, kind, detail in evs[-200:]:
        print(f"  {datetime.fromtimestamp(ts):%m-%d %H:%M:%S} {kind}"
              + (f" {json.dumps(detail)}" if detail else ""))
    store.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = until stopped)")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="dev")
    ap.add_argument("--db", help="override the database path")
    ap.add_argument("--camera", type=int, default=1)
    ap.add_argument("--size", default="640x480")
    ap.add_argument("--report", type=float, metavar="HOURS",
                    help="print the last HOURS of events and row counts, then exit (no camera)")
    args = ap.parse_args()

    size = tuple(int(v) for v in args.size.split("x"))
    db_path = pathlib.Path(args.db) if args.db else HERE / PROFILES[args.profile]
    store = Store(db_path)
    if args.report is not None:
        return report(store, args.report)
    senses = E.Senses(lights_hist=store.load("lights_hist"), decoys=store.load("decoys"))
    eyes = Eyes()

    cap = open_camera(args.camera, size)
    if cap is None:
        log(f"camera {args.camera}: no live frame. Is the dashboard holding it? (./runHome stop)")
        return 1
    log(f"running: {db_path.name}, camera {args.camera} at {args.size}"
        f"{'' if not args.seconds else f', for {args.seconds:.0f} s'}")

    t0 = time.monotonic()
    due = {k: 0.0 for k in RATES}
    vec_due = {k: 0.0 for k in VECTOR_EVERY}
    last_ms = -1
    next_save = t0 + SAVE_LEARNED_S
    n_events, frames, misses = 0, 0, 0

    def save_learned():
        store.save("lights_hist", senses.lights.hist)
        store.save("decoys", [{"box": list(d["box"])} for d in senses.decoys.known])

    try:
        while not args.seconds or time.monotonic() - t0 < args.seconds:
            ok, frame = cap.read()
            if not ok or frame is None:
                misses += 1
                if misses >= 50:
                    log("camera stopped delivering frames; reopening")
                    cap.release()
                    time.sleep(2.0)
                    cap = open_camera(args.camera, size)
                    if cap is None:
                        log("camera lost")
                        return 1
                    misses = 0
                continue
            misses = 0
            frames += 1
            mono, ts = time.monotonic(), time.time()
            ms = max(last_ms + 1, int((mono - t0) * 1000))   # MediaPipe needs strictly increasing
            last_ms = ms
            h, w = frame.shape[:2]
            image = mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            obs = E.Obs(ts=ts, brightness=float(frame.mean()))

            run = {k for k, hz in RATES.items() if mono >= due[k]}
            for k in run:
                due[k] = mono + 0.9 / RATES[k]      # 10 % slack, see bench_mediapipe

            if "hands" in run:
                r = eyes.hands.recognize_for_video(image, ms)
                obs.hands = []
                for i, pts in enumerate(r.hand_landmarks):
                    top = r.gestures[i][0] if r.gestures and r.gestures[i] else None
                    side = r.handedness[i][0].category_name if r.handedness and r.handedness[i] else ""
                    obs.hands.append(E.Hand(top.category_name if top else "None",
                                            top.score if top else 0.0, side))
                    if mono >= vec_due["hand"]:
                        store.vector(ts, "hand", hand_vector(pts))
                if r.hand_landmarks and mono >= vec_due["hand"]:
                    vec_due["hand"] = mono + VECTOR_EVERY["hand"]
            if "body" in run:
                r = eyes.body.detect_for_video(image, ms)
                obs.bodies = [b for b in (body_of(lm, w / h) for lm in r.pose_landmarks) if b]
                if r.pose_landmarks and mono >= vec_due["pose"]:
                    store.vector(ts, "pose", [v for p in r.pose_landmarks[0]
                                              for v in (p.x, p.y, p.z, p.visibility)])
                    vec_due["pose"] = mono + VECTOR_EVERY["pose"]
            if "face" in run:
                r = eyes.face.detect_for_video(image, ms)
                obs.faces = []
                for i, lm in enumerate(r.face_landmarks):
                    blend = {c.category_name: c.score for c in r.face_blendshapes[i]} \
                        if r.face_blendshapes else {}
                    yaw, pitch = head_pose(r.facial_transformation_matrixes[i]) \
                        if r.facial_transformation_matrixes else (90.0, 90.0)
                    obs.faces.append(E.Face(box_of(lm), yaw, pitch, blend.get("jawOpen", 0.0),
                                            (blend.get("eyeBlinkLeft", 0.0) + blend.get("eyeBlinkRight", 0.0)) / 2))
                if r.face_blendshapes and mono >= vec_due["face_blend"]:
                    store.vector(ts, "face_blend", [c.score for c in r.face_blendshapes[0]])
                    vec_due["face_blend"] = mono + VECTOR_EVERY["face_blend"]

            for ev in senses.update(obs):
                store.event(ev)
                n_events += 1
                log(f"{ev.kind}" + (f" {json.dumps(ev.detail)}" if ev.detail else ""))
            row = senses.sample(ts)
            if row:
                store.sample(row)
                store.commit()
            if mono >= next_save:
                save_learned()
                next_save = mono + SAVE_LEARNED_S
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        save_learned()
        elapsed = time.monotonic() - t0
        counts = store.counts(since=time.time() - elapsed - 1)
        thr, learned = senses.lights.threshold()
        log(f"stopped after {elapsed:.0f} s: {frames / max(elapsed, 1e-9):.1f} fps, "
            f"{n_events} events, rows {counts}, lights threshold {thr:.0f} "
            f"({'learned' if learned else 'default'}), decoys known {len(senses.decoys.known)}")
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
