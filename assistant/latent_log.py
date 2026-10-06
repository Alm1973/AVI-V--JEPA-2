"""Record V-JEPA latents of the desk, forever, and never a frame.

Step 2 of docs/LEARNING-DESIGN.md, and the experiment the whole learning stack
is gated on: if these vectors do not cluster into anything resembling his
actual life, nothing built on top of them can work either.

WHAT IT STORES, AND WHY THAT IS THE POINT
A frozen V-JEPA 2 encoder turns a short clip into one 1024-float vector. That
vector is what goes in the database; the pixels are discarded immediately and
nothing is ever written to disk as an image. Three things fall out of that:

  - a month of his life is a few hundred MB instead of terabytes
  - every model above this trains on vectors, in seconds, on this laptop
  - no frame is stored at all, which is stricter than the audit thumbnails

Latents are not images and there is no decoder here to turn them back into
one, but they ARE derived from them and still describe the room. They stay on
the MSI like everything else.

SAMPLING IS REGULAR, NOT ONLY ON CHANGE
change.py exists to fire when something happens, and the obvious thing would
be to log only then. That would be wrong: the predictor above this needs a
TIME SERIES, and a dataset made only of transitions has no examples of nothing
happening -- which is most of a day and is exactly what "he is focused" looks
like. So this samples on a fixed interval and records whether change fired as
a column. Absence of change is data.

    python latent_log.py --hours 8
    python latent_log.py --interval 15 --hours 8
"""
import argparse
import os
import sqlite3
import time
from datetime import datetime

import settings

MODEL_ID = "facebook/vjepa2-vitl-fpc64-256"
CLIP_FRAMES = 16
CLIP_SIZE = 256
LATENT_DB = "latents.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS latents (
  id         INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  local_time TEXT NOT NULL,
  hour       INTEGER NOT NULL,
  weekday    INTEGER NOT NULL,
  changed    INTEGER NOT NULL,
  light_mean REAL,
  light_sd   REAL,
  encode_ms  REAL,
  model      TEXT NOT NULL,
  pool       TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS latents_ts ON latents(ts);
CREATE INDEX IF NOT EXISTS latents_hour ON latents(hour);
"""


def log(msg):
    print(f"[latent {datetime.now():%H:%M:%S}] {msg}", flush=True)


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()
    return db


class Encoder:
    """Frozen V-JEPA 2. Loaded once, never trained."""

    def __init__(self, model_id=MODEL_ID):
        import torch
        from transformers import AutoModel, AutoVideoProcessor
        self._torch = torch
        self.model_id = model_id
        self.proc = AutoVideoProcessor.from_pretrained(model_id)
        self.model = AutoModel.from_pretrained(model_id, dtype=torch.float16)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = self.model.to(self.device).eval()
        # Mean over the token axis. V-JEPA 2 emits (1, 2048, 1024) for a
        # 16-frame clip, i.e. one token per space-time patch, and the mean is
        # the cheapest summary that keeps the whole clip. Recorded in the row
        # so a different pooling later is never silently mixed in with this.
        self.pool = "mean"

    def __call__(self, frames):
        torch = self._torch
        t0 = time.perf_counter()
        inputs = self.proc(frames, return_tensors="pt")
        inputs = {k: (v.to(self.device).half() if v.dtype.is_floating_point
                      else v.to(self.device)) for k, v in inputs.items()}
        with torch.inference_mode():
            out = self.model(**inputs)
        h = out.last_hidden_state                      # (1, tokens, dim)
        vec = h.float().mean(dim=1).squeeze(0).cpu().numpy()
        return vec, (time.perf_counter() - t0) * 1000.0


def grab_clip(cap, n, size):
    """n frames as RGB squares, plus the brightness of the last one.

    Brightness is kept because "no detections" and "the camera is pointed at a
    wall" are indistinguishable without it -- that cost an hour once.
    """
    import cv2
    import numpy as np
    frames = []
    gray = None
    for _ in range(n):
        ok, f = cap.read()
        if not ok:
            return None, None
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        frames.append(cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2RGB),
                                 (size, size), interpolation=cv2.INTER_AREA))
    light = (round(float(np.mean(gray)), 1), round(float(np.std(gray)), 1))
    return frames, light


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=LATENT_DB)
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--interval", type=float, default=15.0,
                    help="seconds between samples")
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--model", default=MODEL_ID)
    args = ap.parse_args()

    import cv2
    from change import ChangeDetector

    db = open_db(args.db)
    have = db.execute("SELECT COUNT(*) c FROM latents").fetchone()["c"]
    log(f"db {os.path.abspath(args.db)} holds {have} latents")

    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, settings.FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, settings.FRAME_HEIGHT)
    if not cap.isOpened():
        log(f"camera {args.camera} would not open")
        return
    log("camera open")

    log(f"loading {args.model} (frozen) ...")
    enc = Encoder(args.model)
    log(f"encoder ready on {enc.device}")

    change = ChangeDetector()
    deadline = time.time() + args.hours * 3600
    n = 0
    try:
        while time.time() < deadline:
            loop_start = time.time()
            frames, light = grab_clip(cap, CLIP_FRAMES, CLIP_SIZE)
            if frames is None:
                log("lost the camera; stopping")
                break
            now = time.time()
            # update() wants a full frame; the last grabbed one is fine and
            # costs nothing extra.
            fired = False
            ok, raw = cap.read()
            if ok:
                fired = bool(change.update(raw, now))
                if fired:
                    change.reset(raw)
            vec, ms = enc(frames)
            lt = datetime.fromtimestamp(now)
            db.execute(
                "INSERT INTO latents (ts, local_time, hour, weekday, changed,"
                " light_mean, light_sd, encode_ms, model, pool, dim, vec)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, lt.strftime("%Y-%m-%d %H:%M:%S"), lt.hour,
                 lt.weekday(), int(fired), light[0], light[1], round(ms, 1),
                 args.model, enc.pool, int(vec.shape[0]),
                 vec.astype("float16").tobytes()))
            db.commit()
            n += 1
            if n == 1 or n % 40 == 0:
                log(f"{n} latents, dim {vec.shape[0]}, {ms:.0f} ms, "
                    f"light {light[0]}/{light[1]}"
                    f"{', change' if fired else ''}")
            sleep = args.interval - (time.time() - loop_start)
            if sleep > 0:
                time.sleep(sleep)
    except KeyboardInterrupt:
        log("stopped by hand")
    finally:
        cap.release()
        total = db.execute("SELECT COUNT(*) c FROM latents").fetchone()["c"]
        log(f"wrote {n} this run; {total} in the db")
        db.close()


if __name__ == "__main__":
    main()
