"""How much does the picture change frame to frame -- still vs. moving?

Run ON THE MSI while the arm is moved. Reads the dashboard's own MJPEG
stream locally and prints one number per frame (mean absolute difference of
consecutive 64x36 grey thumbnails, look_check.thumb). No frame is saved or
sent anywhere; only the numbers leave the machine.

    python still_probe.py [seconds] [url]
"""
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avi_bot.perception import look_check  # noqa: E402

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
if len(sys.argv) > 2:
    url = sys.argv[2]
else:
    from camera_stream import tailscale_ip  # noqa: E402
    url = f"http://{tailscale_ip()}:8000/stream.mjpg"
stream = urllib.request.urlopen(url, timeout=10)
buf, prev, t0 = b"", None, time.time()
while time.time() - t0 < secs:
    buf += stream.read(16384)
    a = buf.find(b"\xff\xd8")
    b = buf.find(b"\xff\xd9", a + 2)
    if a < 0 or b < 0:
        continue
    jpg, buf = buf[a:b + 2], buf[b + 2:]
    f = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if f is None:
        continue
    t = look_check.thumb(f)
    if prev is not None:
        print(f"{time.time() - t0:6.2f} {float(np.abs(t - prev).mean()):6.2f}"
              f" level {float(f.mean()):5.1f}", flush=True)
    prev = t
