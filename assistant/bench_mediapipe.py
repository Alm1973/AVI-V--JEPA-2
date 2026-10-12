"""Can the MSI's CPU carry AVI's three always-on senses at once?

docs/REDESIGN.md puts hands, body and face on MediaPipe, on the CPU, so the
GPU stays free for Qwen. That only works if all three fit together at their
planned rates, and nobody has measured it. This runs them on the live camera
at those rates for a fixed time and reports what was achieved:

    hands  (Gesture Recognizer, 2 hands)        target 15 Hz
    body   (Pose Landmarker lite)                target  5 Hz
    face   (Face Landmarker + blendshapes)       target  5 Hz

    python -u bench_mediapipe.py --seconds 60

Per model: achieved Hz, median and p95 ms. Overall: this process's CPU as a
share of the whole machine, and the machine's total CPU. Also how often a
person was detected, because an empty room is cheaper than an occupied one
(landmark models only run after a detector fires) and the verdict has to say
which it measured.

Frames are never saved or sent. Only numbers are printed.

Needs Python 3.12 with mediapipe, opencv-python, psutil -- NOT .venv-yolo,
which reproduces the paper and takes no new packages. mpbench.ps1 builds that
environment.
"""
import argparse
import pathlib
import statistics
import time
import urllib.request

import cv2
import mediapipe as mp
import psutil
from mediapipe.tasks.python import BaseOptions, vision

MODELS = pathlib.Path(__file__).with_name("models") / "mediapipe"
URLS = {
    "gesture_recognizer.task": "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task",
    "pose_landmarker_lite.task": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
    "face_landmarker.task": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task",
}


def model(name):
    path = MODELS / name
    if not path.is_file():
        MODELS.mkdir(parents=True, exist_ok=True)
        print(f"downloading {name}")
        urllib.request.urlretrieve(URLS[name], path)
    return BaseOptions(model_asset_path=str(path))


def open_camera(index):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    # The C270's first ~5 frames are black with ok=True (CLAUDE.md).
    for _ in range(60):
        ok, frame = cap.read()
        if ok and frame.mean() >= 1.0:
            return cap
    raise SystemExit(f"camera {index}: no live frame (is the dashboard holding it? ./runHome stop)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--camera", type=int, default=1)
    ap.add_argument("--width", type=int, default=640, help="frames are downscaled to this before inference")
    args = ap.parse_args()

    video = vision.RunningMode.VIDEO
    senses = {
        "hands": (15, vision.GestureRecognizer.create_from_options(
            vision.GestureRecognizerOptions(base_options=model("gesture_recognizer.task"),
                                            running_mode=video, num_hands=2)).recognize_for_video,
                  lambda r: bool(r.hand_landmarks)),
        "body": (5, vision.PoseLandmarker.create_from_options(
            vision.PoseLandmarkerOptions(base_options=model("pose_landmarker_lite.task"),
                                         running_mode=video)).detect_for_video,
                 lambda r: bool(r.pose_landmarks)),
        "face": (5, vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(base_options=model("face_landmarker.task"), running_mode=video,
                                         num_faces=2, output_face_blendshapes=True)).detect_for_video,
                 lambda r: bool(r.face_landmarks)),
    }
    ms = {k: [] for k in senses}
    seen = {k: 0 for k in senses}
    due = {k: 0.0 for k in senses}

    cap = open_camera(args.camera)
    me = psutil.Process()
    me.cpu_percent(None)
    psutil.cpu_percent(None)
    cpu_me, cpu_all = [], []
    t0 = last_sample = time.perf_counter()
    frames = 0
    print(f"running {args.seconds:.0f} s on {psutil.cpu_count()} logical CPUs ...")
    while (now := time.perf_counter()) - t0 < args.seconds:
        ok, frame = cap.read()
        if not ok:
            raise SystemExit("camera read failed mid-run")
        frames += 1
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (args.width, round(h * args.width / w)))
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        ts = int((now - t0) * 1000)
        for name, (hz, run, found) in senses.items():
            if now < due[name]:
                continue
            due[name] = now + 1.0 / hz
            s = time.perf_counter()
            result = run(image, ts)
            ms[name].append((time.perf_counter() - s) * 1000)
            seen[name] += found(result)
        if now - last_sample >= 5:
            cpu_me.append(me.cpu_percent(None) / psutil.cpu_count())
            cpu_all.append(psutil.cpu_percent(None))
            last_sample = now
    cap.release()
    elapsed = time.perf_counter() - t0

    print(f"\ncamera frames: {frames / elapsed:.1f} fps")
    print(f"{'sense':6} {'target':>6} {'got':>6} {'median':>8} {'p95':>8} {'detected':>9}")
    for name, (hz, _, _) in senses.items():
        v = sorted(ms[name])
        p95 = v[int(0.95 * (len(v) - 1))]
        print(f"{name:6} {hz:>5}Hz {len(v) / elapsed:>5.1f}Hz {statistics.median(v):>6.1f}ms "
              f"{p95:>6.1f}ms {100 * seen[name] / len(v):>8.0f}%")
    print(f"\nthis process: {statistics.mean(cpu_me):.0f}% of the machine (peak {max(cpu_me):.0f}%)")
    print(f"whole machine: {statistics.mean(cpu_all):.0f}% (peak {max(cpu_all):.0f}%)")
    if seen["body"] < 0.5 * len(ms["body"]):
        print("NOTE: nobody was in view most of the run; an occupied room costs more. Re-run with someone there.")


if __name__ == "__main__":
    main()
