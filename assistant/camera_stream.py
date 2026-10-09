"""Live camera view over Tailscale, for when Shaurya is away from the desk.

BUILD_SPEC 14 asks for a camera stream in the dashboard, "only while Shaurya
allows it". This is that panel, standing alone until the dashboard exists.

    python camera_stream.py --minutes 15

Then open http://msi:8080/ from any device on the tailnet.

PRIVACY, WHICH IS THE WHOLE DESIGN HERE
  - It binds to the MSI's TAILSCALE address only, never 0.0.0.0. Nothing on the
    home LAN, and nothing on the internet, can reach it -- only devices signed
    in to Shaurya's tailnet, which Tailscale has already authenticated and
    encrypted end to end.
  - Frames are held in memory and sent to connected clients. Nothing is written
    to disk, ever. This is not the audit-thumbnail path.
  - It has a hard deadline (--minutes, default 15) and exits on its own. A
    forgotten stream cannot leave the camera live all day.
  - It prints every client connection to the log, so there is a record of who
    watched and when.

The C270 is exclusive through DirectShow, so the tracker and the logger must be
stopped before this runs, and this must be stopped before they start again.
"""
import argparse
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

import settings
from observer import open_camera, read_fresh

BOUNDARY = "avi-frame"


def log(msg):
    print(f"[stream {datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


class Latest:
    """The most recent encoded frame, shared between the grabber and clients.

    One capture thread, many viewers. Two browser tabs opening the camera twice
    would fail on DirectShow, so clients never touch the device.
    """

    def __init__(self):
        self.jpeg = None
        self.seq = 0
        self.lock = threading.Condition()
        self.stop = threading.Event()

    def put(self, jpeg):
        with self.lock:
            self.jpeg = jpeg
            self.seq += 1
            self.lock.notify_all()

    def get_after(self, seq, timeout=5.0):
        """Block until a frame newer than `seq` exists. Returns (jpeg, seq)."""
        with self.lock:
            if not self.lock.wait_for(lambda: self.seq != seq or self.stop.is_set(),
                                      timeout=timeout):
                return None, seq
            if self.stop.is_set():
                return None, seq
            return self.jpeg, self.seq


PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>AVI camera</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#111; color:#ddd; font:14px -apple-system,
         system-ui, sans-serif; display:flex; flex-direction:column;
         align-items:center; gap:12px; padding:16px; }
  img { max-width:100%; height:auto; border-radius:8px; background:#000; }
  .row { display:flex; gap:12px; align-items:center; }
  button { font:inherit; padding:6px 14px; border-radius:6px; border:0;
           background:#333; color:#ddd; cursor:pointer; }
  button:hover { background:#444; }
  code { color:#888; }
</style></head>
<body>
  <img src="/stream.mjpg" alt="camera">
  <div class="row">
    <code id="t"></code>
    <button onclick="fetch('/stop',{method:'POST'}).then(()=>document.body.innerHTML='<p>stopped. the camera is released.</p>')">stop the stream</button>
  </div>
<script>
  const t = document.getElementById('t');
  fetch('/deadline').then(r=>r.json()).then(d=>{
    setInterval(()=>{
      const left = Math.max(0, d.ends_in - (Date.now()-d.at)/1000);
      t.textContent = 'auto-stops in ' + Math.floor(left/60) + 'm ' +
                      String(Math.floor(left%60)).padStart(2,'0') + 's';
    }, 1000);
  });
</script>
</body></html>
"""


def make_handler(latest, deadline, fps):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            pass            # we do our own, and only for the things worth it

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PAGE)))
                # no-store, because this page changes between sessions and a
                # cached copy is indistinguishable from a broken one: the
                # selector was served correctly for a while and simply never
                # reached the browser.
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.end_headers()
                self.wfile.write(PAGE)
            elif self.path == "/deadline":
                body = (b'{"at":%d,"ends_in":%d}'
                        % (int(time.time() * 1000),
                           int(max(0, deadline - time.time()))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/stream.mjpg":
                self.stream()
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/stop":
                log("stop requested from the page")
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                latest.stop.set()
            else:
                self.send_error(404)

        def stream(self):
            log(f"viewer connected from {self.client_address[0]}")
            self.send_response(200)
            self.send_header("Content-Type",
                             f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = 0
            period = 1.0 / max(1.0, fps)
            try:
                while not latest.stop.is_set() and time.time() < deadline:
                    jpeg, seq = latest.get_after(seq)
                    if jpeg is None:
                        continue
                    self.wfile.write(
                        b"--" + BOUNDARY.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode() +
                        b"\r\n\r\n" + jpeg + b"\r\n")
                    time.sleep(period)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                log(f"viewer {self.client_address[0]} disconnected")

    return Handler


def grabber(latest, args, deadline):
    cap = None
    try:
        cap = open_camera(args.camera)
        log("camera open")
        period = 1.0 / max(1.0, args.fps)
        params = [int(cv2.IMWRITE_JPEG_QUALITY), args.quality]
        while not latest.stop.is_set() and time.time() < deadline:
            t0 = time.time()
            ok, frame = read_fresh(cap)
            if not ok or frame is None:
                time.sleep(0.2)
                continue
            if args.width and frame.shape[1] != args.width:
                h = int(frame.shape[0] * args.width / frame.shape[1])
                frame = cv2.resize(frame, (args.width, h),
                                   interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", frame, params)
            if ok:
                latest.put(buf.tobytes())
            time.sleep(max(0.0, period - (time.time() - t0)))
    except Exception as exc:
        log(f"camera failed: {exc!r}")
    finally:
        latest.stop.set()
        if cap is not None:
            cap.release()
        log("camera released")


def tailscale_ip():
    """The MSI's 100.x tailnet address, so we never bind to 0.0.0.0."""
    try:
        import subprocess
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=10)
        ip = out.stdout.strip().splitlines()[0].strip()
        if ip.startswith("100."):
            return ip
    except Exception:
        pass
    # Fall back to whichever local address is in Tailscale's range.
    for info in socket.getaddrinfo(socket.gethostname(), None,
                                   socket.AF_INET):
        ip = info[4][0]
        if ip.startswith("100."):
            return ip
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--quality", type=int, default=70,
                    help="JPEG quality, 1-100")
    ap.add_argument("--width", type=int, default=960,
                    help="downscale width; 0 keeps the native 1280")
    ap.add_argument("--minutes", type=float, default=15.0,
                    help="hard deadline, then it exits and frees the camera")
    ap.add_argument("--bind", default="",
                    help="address to bind; defaults to the Tailscale IP. "
                         "Pass 0.0.0.0 only if you really mean the whole LAN.")
    args = ap.parse_args()

    host = args.bind or tailscale_ip()
    if not host:
        log("could not find a Tailscale address to bind to. Refusing to fall "
            "back to 0.0.0.0, which would expose the camera to the whole LAN. "
            "Check `tailscale status`, or pass --bind explicitly.")
        return 2

    deadline = time.time() + args.minutes * 60.0
    latest = Latest()
    threading.Thread(target=grabber, args=(latest, args, deadline),
                     daemon=True).start()

    srv = ThreadingHTTPServer((host, args.port), make_handler(latest, deadline,
                                                              args.fps))
    srv.daemon_threads = True
    # Without it the deadline is only checked when the next request comes
    # in, and an ended run keeps the camera (dashboard.py, 2026-10-09).
    srv.timeout = 1.0
    log(f"serving http://{host}:{args.port}/  (also http://msi:{args.port}/ "
        f"with MagicDNS) for {args.minutes:.0f} min")
    try:
        while not latest.stop.is_set() and time.time() < deadline:
            srv.handle_request()
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        latest.stop.set()
        srv.server_close()
        log("stopped; camera is free")
    return 0


if __name__ == "__main__":
    sys.exit(main())
