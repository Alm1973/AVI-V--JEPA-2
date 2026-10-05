"""Remote controller: WASD over Tailscale, with the camera feed beside it.

    python teleop.py --minutes 20                  # simulator (default, safe)
    python teleop.py --backend serial --yes-move-the-arm

Then open http://msi:8081/ from any device on the tailnet.

WASD drives the two joints that are actually driven. The camera is on the wrist,
so the feed IS the feedback: if the view moves, the arm moved.

    W / S   shoulder  (view up / down)
    A / D   base yaw  (view left / right)
    Q / E   elbow     } pinned to center until their direction signs are
    R / F   wrist     } measured, so these do nothing. The page says so.
    space   stop dead
    H       go home

BEFORE --backend serial WILL DO ANYTHING, TWO THINGS MUST BE TRUE
  1. firmware/avi_servo_v2 must be flashed to the Uno. The arm currently runs
     v1, which speaks relative "X:<deg> Y:<deg>" lines and would silently
     ignore every "P" frame this sends. Flashing needs physical access.
  2. Somebody should be watching. Opening COM5 RESETS the Uno, and v1 snaps
     both servos to center when that happens. v2 exists to stop exactly that,
     by reading the PCA9685's registers back, but that behaviour is itself
     unverified on the bench.

So serial is behind an explicit flag, the manual envelope is +-15 degrees by
default, and the default backend is the simulator -- which is not a toy here:
the same MotionEngine, limiter and gesture library run in both, so anything
that misbehaves in sim would have misbehaved on the arm.
"""
import argparse
import json
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# avi\assistant\teleop.py -> avi\ , so "import avi_bot.motion" resolves and
# poses.py finds ../config/joints.toml beside it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import settings                                             # noqa: E402
from camera_stream import BOUNDARY, Latest, grabber, tailscale_ip   # noqa: E402

from avi_bot.motion import backends, engine, gestures, poses  # noqa: E402

# key -> (joint, direction). The two signs that are confirmed on hardware:
# base_yaw + moves the view right, shoulder + moves the view down.
KEYMAP = {
    "w": ("shoulder", -1.0), "s": ("shoulder", +1.0),
    "a": ("base_yaw", -1.0), "d": ("base_yaw", +1.0),
    "q": ("elbow", -1.0), "e": ("elbow", +1.0),
    "r": ("wrist", -1.0), "f": ("wrist", +1.0),
}
INPUT_WATCHDOG_S = 0.5


def log(msg):
    print(f"[teleop {datetime.now():%H:%M:%S}] {msg}", flush=True)


class Driver:
    """Turns held keys into a bounded manual offset from center.

    Keys set a velocity; the motion thread integrates it. That way the arm
    moves smoothly while a key is down and stops when it is released, and the
    speed cap in safety.Limiter still has the last word.

    The watchdog matters more than it looks: a dropped keyup, a closed laptop
    lid or a lost tailnet would otherwise leave a key held down forever. If no
    input arrives for half a second the velocity goes to zero, which is the
    same rule the firmware applies to the host.
    """

    def __init__(self, eng, max_deg, speed_deg_s, clock=time.time):
        self.eng = eng
        self.max_deg = float(max_deg)
        self.speed = float(speed_deg_s)
        # One clock for the whole class. The watchdog compares a timestamp taken
        # in set_held against the `now` integrate is given, so those two must
        # come from the same source or the watchdog fires on clock skew rather
        # than on missing input.
        self.clock = clock
        self.held = set()
        self.manual = {j: 0.0 for j in poses.JOINTS}
        self.last_input = 0.0
        self.lock = threading.Lock()

    def set_held(self, keys):
        with self.lock:
            self.held = {k for k in keys if k in KEYMAP}
            self.last_input = self.clock()

    def stop(self):
        with self.lock:
            self.held = set()
            self.last_input = self.clock()

    def home(self):
        with self.lock:
            self.held = set()
            self.manual = {j: 0.0 for j in poses.JOINTS}
            self.last_input = self.clock()

    def integrate(self, now, dt):
        with self.lock:
            held = set() if (now - self.last_input) > INPUT_WATCHDOG_S \
                else set(self.held)
            for key in held:
                joint, direction = KEYMAP[key]
                v = self.manual[joint] + direction * self.speed * dt
                self.manual[joint] = max(-self.max_deg, min(self.max_deg, v))
            return dict(self.manual)


def motion_loop(eng, driver, stop_event, deadline, hz, pause=None):
    period = 1.0 / hz
    last = time.time()
    while not stop_event.is_set() and time.time() < deadline:
        now = time.time()
        if pause is not None and pause.is_set():
            # A calibration move is deliberately NOT followed by P frames; if
            # this loop kept streaming them it would overwrite the calibration
            # target 50 times a second and the joint would never move.
            last = now
            time.sleep(period)
            continue
        dt = max(1e-4, now - last)
        last = now
        manual = driver.integrate(now, dt)
        # Only steer the base while no gesture is running; a gesture owns the
        # arm for its duration and manual control resumes after it.
        if not eng.busy():
            eng.base = {j: 90.0 + manual[j] for j in poses.JOINTS}
        eng.tick(now)
        time.sleep(max(0.0, period - (time.time() - now)))
    log("motion loop ended")


PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>AVI remote</title>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;background:#0e0e10;color:#e4e4e7;font:14px -apple-system,system-ui,sans-serif;
      display:flex;flex-direction:column;align-items:center;gap:14px;padding:16px}
 #banner{width:100%;max-width:980px;padding:8px 12px;border-radius:8px;font-weight:600;text-align:center}
 .sim{background:#1e3a5f;color:#bfdbfe}
 .live{background:#7f1d1d;color:#fecaca}
 .wrap{display:flex;gap:16px;flex-wrap:wrap;justify-content:center;width:100%;max-width:980px}
 img{width:620px;max-width:100%;height:auto;border-radius:10px;background:#000}
 .panel{flex:1;min-width:260px;display:flex;flex-direction:column;gap:10px}
 table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
 td{padding:3px 6px;border-bottom:1px solid #27272a}
 td:last-child{text-align:right;color:#a1a1aa}
 .pinned td{color:#52525b}
 .keys{display:grid;grid-template-columns:repeat(3,44px);gap:6px;justify-content:center}
 kbd{display:flex;align-items:center;justify-content:center;height:40px;border-radius:7px;
     background:#27272a;border:1px solid #3f3f46;font:600 14px ui-monospace,monospace}
 kbd.on{background:#3b82f6;border-color:#60a5fa;color:#fff}
 .g{display:flex;flex-wrap:wrap;gap:6px}
 button{font:inherit;padding:6px 11px;border-radius:7px;border:1px solid #3f3f46;
        background:#27272a;color:#e4e4e7;cursor:pointer}
 button:hover{background:#3f3f46}
 button.danger{background:#7f1d1d;border-color:#991b1b}
 code{color:#71717a;font-size:12px}
</style></head>
<body>
 <div id="banner">connecting...</div>
 <div class="wrap">
  <img src="/stream.mjpg" alt="camera">
  <div class="panel">
   <svg id="sim" viewBox="0 0 260 150" width="100%"
        style="background:#18181b;border-radius:10px"></svg>
   <table id="joints"></table>
   <div class="keys">
     <span></span><kbd id="k-w">W</kbd><span></span>
     <kbd id="k-a">A</kbd><kbd id="k-s">S</kbd><kbd id="k-d">D</kbd>
   </div>
   <code id="hint">click here, then hold WASD. space = stop, H = home.</code>
   <div class="g" id="gestures"></div>
   <button class="danger" onclick="post('/stop')">stop the session</button>
   <code id="dl"></code>
  </div>
 </div>
<script>
const held = new Set();
const KEYS = ["w","a","s","d","q","e","r","f"];
function post(p, body){ return fetch(p,{method:"POST",
  headers:{"Content-Type":"application/json"}, body: body?JSON.stringify(body):null}); }
function paint(){ for (const k of ["w","a","s","d"]) {
  const el = document.getElementById("k-"+k); if(el) el.classList.toggle("on", held.has(k)); } }
addEventListener("keydown", e => {
  const k = e.key.toLowerCase();
  if (k === " ") { held.clear(); post("/stop_motion"); e.preventDefault(); paint(); return; }
  if (k === "h") { held.clear(); post("/home"); paint(); return; }
  if (KEYS.includes(k) && !held.has(k)) { held.add(k); paint(); e.preventDefault(); }
});
addEventListener("keyup", e => {
  const k = e.key.toLowerCase();
  if (held.delete(k)) { paint(); e.preventDefault(); }
});
addEventListener("blur", () => { held.clear(); paint(); });
// Heartbeat rather than fire-and-forget on each key: if this stops arriving the
// server's watchdog zeroes the velocity, so a lost keyup cannot run the arm on.
setInterval(() => post("/input", {keys:[...held]}), 150);
let mode = null;
setInterval(async () => {
  const s = await (await fetch("/state")).json();
  if (mode !== s.backend) {
    mode = s.backend;
    const b = document.getElementById("banner");
    const live = s.backend === "serial";
    b.className = live ? "live" : "sim";
    b.textContent = live ? "LIVE - this is moving the real arm"
                         : "SIMULATOR - the arm is not moving";
  }
  draw(s);
  document.getElementById("joints").innerHTML = s.joints.map(j =>
    `<tr class="${j.driven?"":"pinned"}"><td>${j.name}${j.driven?"":" (pinned)"}</td>`+
    `<td>${j.deg.toFixed(1)}&deg; / ${j.count}</td></tr>`).join("");
  const left = Math.max(0, s.ends_in);
  document.getElementById("dl").textContent =
    "auto-stops in " + Math.floor(left/60) + "m " + String(Math.floor(left%60)).padStart(2,"0") + "s";
}, 400);
// Side view on the left, top view on the right. Angles are degrees with 90 as
// the calibrated center, so everything is drawn relative to 90.
function draw(s){
  const g = {}; for (const j of s.joints) g[j.name] = j.deg;
  const L = s.sim || {};
  const l1 = (L.link_shoulder_mm||90)/2.4, l2 = (L.link_elbow_mm||70)/2.4,
        l3 = (L.link_wrist_mm||40)/2.4, bh = (L.base_height_mm||55)/2.4;
  const rad = d => d*Math.PI/180;
  // --- side view: shoulder lifts, elbow and wrist follow ---
  const ox=60, oy=120;
  const a1 = rad(g.shoulder-90), a2 = a1 + rad(g.elbow-90), a3 = a2 + rad(g.wrist-90);
  const x1=ox, y1=oy-bh;
  const x2=x1+Math.sin(a1)*0+l1*Math.cos(a1-Math.PI/2)*0 + l1*Math.sin(a1), y2=y1-l1*Math.cos(a1);
  const x3=x2+l2*Math.sin(a2), y3=y2-l2*Math.cos(a2);
  const x4=x3+l3*Math.sin(a3), y4=y3-l3*Math.cos(a3);
  // --- top view: base yaw ---
  const tx=195, ty=75, yaw=rad(g.base_yaw-90), R=42;
  const ex=tx+R*Math.sin(yaw), ey=ty-R*Math.cos(yaw);
  const seg=(xa,ya,xb,yb,c,w)=>`<line x1="${xa}" y1="${ya}" x2="${xb}" y2="${yb}" stroke="${c}" stroke-width="${w}" stroke-linecap="round"/>`;
  document.getElementById("sim").innerHTML =
    `<text x="8" y="14" fill="#52525b" font-size="9">side</text>`+
    `<text x="150" y="14" fill="#52525b" font-size="9">top (yaw)</text>`+
    seg(20,oy,100,oy,"#3f3f46",2)+
    seg(ox,oy,x1,y1,"#52525b",7)+
    seg(x1,y1,x2,y2,"#60a5fa",6)+
    seg(x2,y2,x3,y3,"#3b82f6",5)+
    seg(x3,y3,x4,y4,"#93c5fd",4)+
    `<circle cx="${x4}" cy="${y4}" r="4.5" fill="#fbbf24"/>`+
    `<text x="${x4+7}" y="${y4+3}" fill="#a1a1aa" font-size="8">cam</text>`+
    `<circle cx="${tx}" cy="${ty}" r="${R}" fill="none" stroke="#27272a" stroke-width="1"/>`+
    seg(tx,ty,ex,ey,"#60a5fa",5)+
    `<circle cx="${ex}" cy="${ey}" r="4" fill="#fbbf24"/>`+
    `<circle cx="${tx}" cy="${ty}" r="3" fill="#52525b"/>`;
}
fetch("/gestures").then(r=>r.json()).then(gs => {
  document.getElementById("gestures").innerHTML = gs.map(g =>
    `<button onclick="post('/gesture',{name:'${g}'})">${g}</button>`).join("");
});
</script>
</body></html>
"""


def make_handler(state):
    latest, eng, driver, deadline, fps, stop_event = (
        state["latest"], state["engine"], state["driver"],
        state["deadline"], state["fps"], state["stop"])
    pause = state["pause"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PAGE)))
                self.end_headers()
                self.wfile.write(PAGE)
            elif self.path == "/state":
                cfg = eng.config
                sent = eng.last_sent
                self._json({
                    "backend": eng.backend.name,
                    "ends_in": max(0.0, deadline - time.time()),
                    "busy": eng.busy(),
                    "joints": [{"name": j, "deg": sent[j],
                                "count": round(cfg.deg_to_count(j, sent[j])),
                                "driven": cfg.is_driven(j)}
                               for j in poses.JOINTS],
                    # Link lengths so the page can draw the arm. Eyeballed, not
                    # measured: they make the picture roughly right, and the
                    # picture is there to show that a key did something.
                    "sim": cfg.sim,
                })
            elif self.path == "/gestures":
                self._json(sorted(gestures.GESTURES))
            elif self.path == "/stream.mjpg":
                self.stream()
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/input":
                driver.set_held(self._read_json().get("keys") or [])
                self._json({"ok": True})
            elif self.path == "/stop_motion":
                driver.stop()
                eng.clear()
                self._json({"ok": True})
            elif self.path == "/home":
                driver.home()
                eng.clear()
                self._json({"ok": True})
            elif self.path == "/gesture":
                name = (self._read_json().get("name") or "").strip()
                if name not in gestures.GESTURES:
                    return self._json({"error": "unknown gesture"}, 400)
                log(f"gesture {name}")
                eng.push(gestures.make(name))
                self._json({"ok": True})
            elif self.path == "/calib":
                body = self._read_json()
                ch = int(body.get("channel", -1))
                deg = float(body.get("deg", 90.0))
                if not (0 <= ch < len(poses.JOINTS)):
                    return self._json({"error": "channel 0..3"}, 400)
                if not hasattr(eng.backend, "calibrate"):
                    return self._json({"error": "not a serial backend"}, 400)
                log(f"CALIB channel {ch} ({poses.JOINTS[ch]}) -> {deg:.1f} deg")
                pause.set()                    # stop the P stream
                time.sleep(0.15)
                eng.backend.calibrate(ch, deg)
                self._json({"ok": True, "joint": poses.JOINTS[ch], "deg": deg})
            elif self.path == "/resume":
                pause.clear()
                driver.home()
                self._json({"ok": True})
            elif self.path == "/cap":
                v = float(self._read_json().get("deg_s", 60.0))
                if not hasattr(eng.backend, "set_speed_cap"):
                    return self._json({"error": "not a serial backend"}, 400)
                log(f"speed cap -> {v:.1f} deg/s")
                eng.backend.set_speed_cap(v)
                self._json({"ok": True, "deg_s": v})
            elif self.path == "/stop":
                log("session stop requested from the page")
                self._json({"ok": True})
                stop_event.set()
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


def build_backend(args, config):
    if args.backend == "sim":
        return backends.SimBackend(config)
    if args.backend == "dry-run":
        return backends.DryRunBackend()
    if args.backend == "serial":
        if not args.yes_move_the_arm:
            raise SystemExit(
                "refusing to open the serial port without --yes-move-the-arm.\n"
                "Opening COM5 resets the Uno, and the v1 firmware snaps both\n"
                "servos to center when that happens, with nobody there to watch.\n"
                "Flash firmware/avi_servo_v2 first, and have someone watching.")
        log(f"OPENING {args.port_name} - the Uno will reset and may move")
        return backends.SerialBackend(args.port_name, config).open()
    raise SystemExit(f"unknown backend {args.backend!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--backend", default="sim",
                    choices=("sim", "dry-run", "serial"))
    ap.add_argument("--port-name", default="COM5")
    ap.add_argument("--yes-move-the-arm", action="store_true",
                    help="required for --backend serial; it moves real servos")
    ap.add_argument("--max-deg", type=float, default=15.0,
                    help="manual envelope either side of center. BUILD_SPEC "
                         "says 15 degrees maximum for an unattended test.")
    ap.add_argument("--speed", type=float, default=12.0,
                    help="degrees per second while a key is held")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--no-camera", action="store_true",
                    help="controller only, for when the camera is busy")
    ap.add_argument("--bind", default="")
    args = ap.parse_args()

    host = args.bind or tailscale_ip()
    if not host:
        log("no Tailscale address to bind to; refusing to fall back to 0.0.0.0 "
            "because that would expose the camera to the whole LAN")
        return 2

    config = poses.load()
    backend = build_backend(args, config)

    # Start from where the arm actually is, not from center.
    #
    # The firmware does its half of this by reading the PCA9685 registers back
    # on boot; this is the host's half. Without it, the first P frame of every
    # live session asks for 90 and the arm walks there from wherever it was
    # left -- a 7-degree lurch in the first test, which is exactly the startup
    # jump both halves exist to prevent.
    start = "HOME"
    state = backend.read_state()
    if state:
        start = state[0]
        log("arm reports " + " ".join(f"{j}={start[j]:.1f}" for j in poses.JOINTS)
            + f" (firmware {state[1]}) - starting there, not at center")
    eng = engine.MotionEngine(config, backend, start_pose=start)
    # Breathing and saccades are personality, not teleop. Off here so that what
    # moves is only what the keys asked for -- otherwise a live test cannot
    # tell a command from an idle animation.
    for layer in eng.layers:
        layer.weight = 0.0
    driver = Driver(eng, args.max_deg, args.speed)
    # Seed the manual offsets from that same reading, or the first motion tick
    # would overwrite the start pose with center and undo it.
    for j in poses.JOINTS:
        driver.manual[j] = max(-args.max_deg,
                               min(args.max_deg, eng.base[j] - 90.0))

    deadline = time.time() + args.minutes * 60.0
    latest = Latest()
    stop_event = threading.Event()

    if not args.no_camera:
        threading.Thread(target=grabber, args=(latest, args, deadline),
                         daemon=True).start()
    pause = threading.Event()
    threading.Thread(target=motion_loop,
                     args=(eng, driver, stop_event, deadline,
                           config.tick_hz, pause), daemon=True).start()

    srv = ThreadingHTTPServer((host, args.port), make_handler({
        "latest": latest, "engine": eng, "driver": driver,
        "deadline": deadline, "fps": args.fps, "stop": stop_event,
        "pause": pause}))
    srv.daemon_threads = True
    log(f"backend={backend.name} envelope=+-{args.max_deg:g} deg "
        f"speed={args.speed:g} deg/s")
    log(f"serving http://{host}:{args.port}/ (http://msi:{args.port}/) "
        f"for {args.minutes:.0f} min")
    try:
        while not stop_event.is_set() and time.time() < deadline:
            srv.handle_request()
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        stop_event.set()
        latest.stop.set()
        srv.server_close()
        time.sleep(0.2)
        backend.home()
        backend.close()
        log("stopped; arm asked to go home, camera released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
