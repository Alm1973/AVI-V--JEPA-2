"""The dance and babbling code end to end, with no arm and no camera.

Run ON THE MSI (it needs OpenCV, like the dashboard). Builds a dashboard
Arm on the simulator backend and a synthetic camera: a fixed textured
"room" seen through a window that moves with where the body model says
the camera points (motion/body.py). Then:

  1. Arm.perform on a composed dolphin dance: must say moved, and back
  2. Arm.perform on a dance that holds still: must say NOT moved
  3. babble(): the measured slopes must have the signs the synthetic
     world was built with -- which checks every sign convention between
     a joint command, ORB's content motion and selfmodel's predictions

Nothing real moves and nothing is saved (babble save=False). Prints
numbers only. Exit status 0 when every check passed.

    python perform_sim_check.py
"""
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avi_bot.dashboard import server                           # noqa: E402
from avi_bot.motion import backends, body as body_mod          # noqa: E402
from avi_bot.motion import choreo, poses                       # noqa: E402
from avi_bot.perception import selfmodel                       # noqa: E402

W, H = 1280, 720
PX = selfmodel.PX_PER_DEG


def make_room(seed=7):
    """A big textured picture: gradient, rectangles, noise -- something
    ORB can lock on to everywhere."""
    rng = np.random.default_rng(seed)
    rw, rh = 4200, 3000
    yy, xx = np.mgrid[0:rh, 0:rw]
    room = np.dstack([(xx * 255 // rw), (yy * 255 // rh),
                      ((xx + yy) * 255 // (rw + rh))]).astype(np.uint8)
    for _ in range(900):
        x, y = int(rng.integers(0, rw - 80)), int(rng.integers(0, rh - 80))
        w, h = int(rng.integers(15, 140)), int(rng.integers(15, 140))
        c = tuple(int(v) for v in rng.integers(0, 255, 3))
        cv2.rectangle(room, (x, y), (x + w, y + h), c, -1)
    room = cv2.add(room, rng.integers(0, 25, room.shape, dtype=np.uint8))
    return room


class SynthCamera:
    """`latest` for the dashboard: get_after(seq, timeout) -> (jpeg, seq),
    a new frame every 1/15 s rendered from the arm's sent pose."""

    def __init__(self, arm, body, room):
        self.arm, self.body, self.room = arm, body, room
        self.seq = 0
        self.home = poses.get_pose("ATTENTIVE")
        self.lock = threading.Lock()

    def render(self):
        pose = dict(self.arm.sent)
        az = self.body.azimuth(pose) - self.body.azimuth(self.home)
        el = self.body.elevation(pose) - self.body.elevation(self.home)
        rh, rw = self.room.shape[:2]
        cx = rw / 2 + az * PX            # turning right moves the window right
        cy = rh / 2 - el * PX            # looking up moves it up
        x0 = int(round(cx - W / 2))
        y0 = int(round(cy - H / 2))
        x0 = max(0, min(rw - W, x0))
        y0 = max(0, min(rh - H, y0))
        return self.room[y0:y0 + H, x0:x0 + W]

    def get_after(self, after, timeout=2.0):
        time.sleep(1 / 15.0)
        with self.lock:
            self.seq += 1
            ok, jpg = cv2.imencode(".jpg", self.render())
            return jpg.tobytes(), self.seq


def main():
    fails = []

    def check(name, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}".rstrip(),
              flush=True)
        if not ok:
            fails.append(name)

    cfg = poses.load()
    body = body_mod.load()
    seat = poses.get_pose("ATTENTIVE")
    arm = server.Arm(cfg, backends.SimBackend(cfg), start=seat)
    threading.Thread(target=arm.run, daemon=True).start()
    cam = SynthCamera(arm, body, make_room())
    arm.latest = cam
    time.sleep(0.5)

    moves, _ = choreo.improvise("dolphin")
    move, info = choreo.compose(moves, seat, config=cfg, body=body,
                                name="sim dolphin")
    got = arm.perform(move, "sim dolphin")
    check("a dolphin dance is seen moving", got.get("moved") is True,
          f"diff max {got.get('max_diff')}")
    check("and seen coming back", got.get("back") is True,
          f"end diff {got.get('final_diff')}")

    still, _ = choreo.compose([{"move": "pause", "times": 4}], seat,
                              config=cfg, body=body, name="sim still")
    got = arm.perform(still, "sim still")
    check("a dance that holds still is NOT seen moving",
          got.get("moved") is False, f"diff max {got.get('max_diff')}")

    out = server.babble(arm, cam, n=8, max_deg=6.0, seed=1, save=False)
    rows = out["rows"]
    check("babbling measured every move", len(rows) == 16, f"{len(rows)}")
    learned = selfmodel.slopes(rows, "orb")
    for j in poses.JOINTS:
        to = dict(seat, **{j: seat[j] + 1.0})
        want = selfmodel.predicted_px(body, seat, to)
        got_j = learned.get(j)
        if got_j is None:
            check(f"{j} slope measured", False)
            continue
        big = 0 if abs(want[0]) >= abs(want[1]) else 1
        k = got_j["dx"] if big == 0 else got_j["dy"]
        check(f"{j}: picture slides the way the world was built",
              (k > 0) == (want[big] > 0) and abs(k - want[big]) <
              0.35 * abs(want[big]) + 3,
              f"learned {got_j} vs built {tuple(round(v, 1) for v in want)}")
    rep = out["compare"]["orb"]
    for j, v in rep["verdict"].items():
        print(f"      {j}: {v}")
    arm.stop.set()
    print("ALL PASSED" if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
