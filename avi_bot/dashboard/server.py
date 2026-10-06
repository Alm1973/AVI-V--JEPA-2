"""The AVI dashboard: camera, joints, poses, gestures, in one page.

    python dashboard.py --minutes 60

Then open http://msi:8000/ from any device on the tailnet.

This is BUILD_SPEC section 14's dashboard, and it is the fold of three separate
tools that each did part of the job: the camera stream, teleop's driving, and
calibrate's pose teaching.

IT IS BUILT ON calibrate's INTERACTION MODEL, NOT teleop's, on purpose.
teleop drove the arm with held keys and a velocity that integrated while a key
was down. That is right for flying an arm around and wrong for everything else
here: choosing a joint, then holding a key and hoping to let go in the right
place, was miserable enough that Shaurya said so in plain terms. So:

  - ONE SLIDER PER SERVO, absolute. Drag it and the joint goes there. There is
    no selected joint, because every slider already is one.
  - No velocity, no integration, no envelope. The only limits are each joint's
    soft limits, straight from config/joints.toml, enforced again in firmware.
  - The firmware eases toward the target under its own speed cap, so dragging
    fast cannot snap anything.
  - Picture quality over frame rate, because aiming a camera needs detail.

Gestures run through the real MotionEngine, so what plays here is exactly what
plays in the sim and on the arm in M2.

Nothing is written to disk except the taught poses, and no frame leaves the MSI
except to the tailnet address this binds to.
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "assistant"))   # settings, camera_stream

import settings                                                   # noqa: E402
from camera_stream import BOUNDARY, Latest, grabber, tailscale_ip  # noqa: E402

from avi_bot.motion import (backends, engine, gestures, poses,     # noqa: E402
                            safety)

TAUGHT_PATH = os.path.join(os.environ.get("USERPROFILE", "."),
                           "avi-remote", "taught_poses.json")

# The poses worth teaching, with what each one is for. Shown in this order.
WANTED = [
    ("HOME", "neutral resting posture; where it returns to"),
    ("OBSERVE", "watching the desk. The logger records object positions ONLY "
                "from this pose, so aim it at the desk properly"),
    ("ATTENTIVE", "slightly raised, looking at where you sit"),
    ("TALL", "alert and upright"),
    ("LOW", "shy or sulking; down and small"),
    ("REST", "asleep; folded as low as it goes, camera down"),
]


def log(msg):
    print(f"[calib {datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_taught():
    try:
        with open(TAUGHT_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_taught(data):
    os.makedirs(os.path.dirname(TAUGHT_PATH), exist_ok=True)
    tmp = TAUGHT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, TAUGHT_PATH)        # atomic: a crash cannot truncate it


class Arm:
    """The current target for every joint, the gesture engine, and the sender.

    Absolute targets, plus a MotionEngine for gestures. The firmware needs a P
    frame at least every 500 ms or its watchdog holds position, so a thread
    repeats the current target at a steady rate whether or not it changed.

    A gesture owns the arm while it plays: the sliders stop writing the base
    pose until it finishes, and then resume from wherever it left the arm. The
    alternative -- sliders rewriting the base 50 times a second underneath a
    running gesture -- is what made driving and posing fight each other in the
    tool this replaced.
    """

    def __init__(self, config, backend, start=None):
        self.config = config
        self.backend = backend
        self.limiter = safety.Limiter(config, start=start,
                                      drive_undriven=False)
        self.target = dict(start or poses.get_pose("HOME"))
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.sent = dict(self.target)
        self.events = []
        self.engine = engine.MotionEngine(config, backend, start_pose=self.target)
        # Breathing and saccades belong to the personality engine in M2. Here
        # they would add motion nobody asked for while a pose is being set.
        for layer in self.engine.layers:
            layer.weight = 0.0

    def note(self, msg):
        """A short ring buffer of what happened, shown in the page."""
        stamp = datetime.now().strftime("%H:%M:%S")
        self.events.append(f"{stamp}  {msg}")
        del self.events[:-40]
        log(msg)

    def busy(self):
        return self.engine.busy()

    def play(self, name):
        move = gestures.make(name)
        self.engine.push(move)
        self.note(f"gesture {name}")
        return move

    def set(self, joint, deg):
        lo, hi = self.config.soft_limits(joint)
        deg = max(lo, min(hi, float(deg)))
        with self.lock:
            self.target[joint] = deg
        return deg

    def set_pose(self, pose):
        for j in poses.JOINTS:
            if j in pose:
                self.set(j, pose[j])

    def snapshot(self):
        with self.lock:
            return dict(self.target)

    def link_status(self):
        """'ok', or why the arm cannot be reached."""
        be = self.backend
        if not hasattr(be, "healthy"):
            return "ok"                      # sim and dry-run cannot fail
        if be.healthy:
            return "ok"
        return be.last_error or "serial link down"

    def run(self, hz=None):
        """Repeat the current target forever, and play gestures.

        Wrapped because this thread dying is invisible from outside: the page
        stays up, the firmware holds position exactly as its watchdog should,
        and the arm simply never moves again. That is what happened on
        2026-10-05 when a serial write raised and nothing caught it.
        """
        hz = hz or self.config.tick_hz
        period = 1.0 / hz
        while not self.stop.is_set():
            now = time.time()
            try:
                if self.paused.is_set():
                    # Hold position: stop sending, and let the firmware's own
                    # watchdog do the holding rather than inventing a second
                    # mechanism for it.
                    time.sleep(period)
                    continue
                if not self.engine.busy():
                    self.engine.base = self.snapshot()
                else:
                    # Keep the sliders in step with where a gesture left the
                    # arm, so releasing control does not snap it back.
                    with self.lock:
                        self.target = dict(self.engine.base)
                self.engine.tick(now)
                self.sent = self.engine.last_sent
            except Exception as exc:
                self.note(f"arm loop error (continuing): {exc!r}")
                time.sleep(0.5)
            time.sleep(max(0.0, period - (time.time() - now)))
        log("arm loop ended")


PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
  content="width=device-width,initial-scale=1"><title>AVI dashboard</title>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;background:#0b0b0d;color:#e7e7ea;
      font:15px/1.45 -apple-system,system-ui,sans-serif;padding:14px;
      max-width:1180px;margin:0 auto}
 h1{font-size:16px;margin:0 0 10px;font-weight:600}
 h1 span{color:#71717a;font-weight:400;font-size:13px}
 .cols{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap}
 .cam{flex:1 1 560px;min-width:320px}
 .cam img{width:100%;border-radius:10px;background:#000;display:block}
 .side{flex:1 1 380px;min-width:300px}
 .j{margin:0 0 14px}
 .j .top{display:flex;justify-content:space-between;align-items:baseline;
         margin-bottom:5px}
 .j .nm{font-weight:600}
 .j .val{font:13px ui-monospace,monospace;color:#a1a1aa}
 input[type=range]{width:100%;height:30px;margin:0;accent-color:#3b82f6}
 .j .btns{display:flex;gap:5px;margin-top:4px}
 .j .btns button{flex:1;padding:5px 0;font:12px ui-monospace,monospace}
 button{font:inherit;padding:7px 13px;border-radius:7px;border:1px solid #3a3a42;
        background:#232329;color:#e7e7ea;cursor:pointer}
 button:hover{background:#32323a}
 button.pri{background:#1d4ed8;border-color:#2563eb;color:#fff}
 button.ok{border-color:#15803d}
 .pose{display:flex;gap:8px;align-items:center;padding:8px 0;
       border-top:1px solid #232329}
 .pose .meta{flex:1;min-width:0}
 .pose .nm{font-weight:600}
 .pose .why{color:#71717a;font-size:12px}
 .pose .saved{font:11px ui-monospace,monospace;color:#4ade80}
 .bar{display:flex;gap:8px;align-items:center;margin:12px 0 0;flex-wrap:wrap}
 code{color:#71717a;font-size:12px}
 #msg{min-height:18px;font-size:13px;color:#fbbf24}
 .sec{margin:16px 0 7px;font-size:12px;text-transform:uppercase;
      letter-spacing:.06em;color:#71717a;border-top:1px solid #232329;
      padding-top:9px}
 .g{display:flex;flex-wrap:wrap;gap:5px}
 .g button{padding:5px 10px;font-size:13px}
 #events{font:11px ui-monospace,monospace;color:#71717a;white-space:pre-wrap;
         max-height:150px;overflow:auto;margin:0}
 #gbusy{color:#60a5fa;text-transform:none;letter-spacing:0}
 body.paused{outline:3px solid #b45309;outline-offset:-3px}
 #link{display:none;background:#7f1d1d;color:#fecaca;padding:7px 11px;
       border-radius:8px;margin:0 0 10px;font-size:13px;font-weight:600}
 #link.show{display:block}
</style></head>
<body>
 <h1>AVI <span id="sub"></span></h1>
 <div id="link"></div>
 <div class="cols">
  <div class="cam">
    <img src="/stream.mjpg" alt="camera">
    <div class="bar">
      <button onclick="allCentre()">centre everything</button>
      <button onclick="selftest()">check every servo moves</button>
      <button id="pz" onclick="togglePause()">pause</button>
      <code id="dl"></code>
    </div>
    <div id="msg"></div>
  </div>
  <div class="side">
    <div id="joints"></div>
    <div class="sec">gestures <span id="gbusy"></span></div>
    <div class="g" id="gestures"></div>
    <div class="sec">poses</div>
    <div id="poses"></div>
    <div class="sec">recent</div>
    <pre id="events"></pre>
    <div class="bar"><code id="build"></code></div>
  </div>
 </div>
<script>
let J = [];
let busy = false;

async function api(path, body){
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)});
  return r.json();
}
function msg(s){ document.getElementById("msg").textContent = s || ""; }

function row(j){
  return `<div class="j" id="j-${j.name}">
    <div class="top"><span class="nm">${j.name}</span>
      <span class="val" id="v-${j.name}">${j.deg.toFixed(1)}&deg; &middot; ${j.count}</span></div>
    <input type="range" id="s-${j.name}" min="${j.lo}" max="${j.hi}" step="0.5"
           value="${j.deg}" oninput="slide('${j.name}', this.value)">
    <div class="btns">
      <button onclick="bump('${j.name}',-5)">-5</button>
      <button onclick="bump('${j.name}',-1)">-1</button>
      <button onclick="bump('${j.name}',1)">+1</button>
      <button onclick="bump('${j.name}',5)">+5</button>
      <button onclick="setJ('${j.name}',90)">90</button>
    </div></div>`;
}

let pending = {};
let flushing = false;
function slide(name, v){
  // Coalesce: a dragged slider fires far faster than the arm can be told, and
  // every event firing its own request just queues work behind the arm.
  pending[name] = parseFloat(v);
  document.getElementById("v-" + name).textContent = parseFloat(v).toFixed(1) + "\\u00b0";
  if (flushing) return;
  flushing = true;
  setTimeout(async () => {
    const batch = pending; pending = {}; flushing = false;
    for (const k in batch) await api("/set", {joint: k, deg: batch[k]});
  }, 60);
}
async function setJ(name, deg){
  document.getElementById("s-" + name).value = deg;
  await api("/set", {joint: name, deg: deg});
}
async function bump(name, d){
  const el = document.getElementById("s-" + name);
  await setJ(name, Math.max(+el.min, Math.min(+el.max, (+el.value) + d)));
}
async function allCentre(){
  for (const j of J) await setJ(j.name, 90);
  msg("everything centred");
}

function poseRow(p, taught){
  const t = taught[p.name];
  const saved = t ? ["base_yaw","shoulder","elbow","wrist"]
      .map(k => k[0] + ":" + t[k]).join("  ") : "";
  return `<div class="pose">
    <div class="meta"><div class="nm">${p.name}</div>
      <div class="why">${p.why}</div>
      ${t ? `<div class="saved">${saved}</div>` : ""}</div>
    ${t ? `<button onclick="recall('${p.name}')">go to</button>` : ""}
    <button class="pri ${t ? "ok" : ""}" onclick="save('${p.name}')">
      ${t ? "re-save" : "save"}</button></div>`;
}
async function save(name){
  const r = await api("/save", {name: name});
  msg("saved " + name + ": " + ["base_yaw","shoulder","elbow","wrist"]
      .map(k => k[0] + ":" + r.pose[k]).join("  "));
  await refresh();
}
async function recall(name){
  await api("/recall", {name: name});
  msg("moving to " + name);
}
async function selftest(){
  if (busy) return;
  busy = true;
  msg("checking each servo in turn, watch the picture ...");
  const r = await api("/selftest", {});
  busy = false;
  msg(r.lines.join("   |   "));
  await refresh();
}

async function playGesture(n){
  await api("/gesture", {name: n});
  msg("playing " + n);
}
async function togglePause(){
  const r = await api("/pause", {});
  msg(r.paused ? "paused: the arm is holding position"
               : "resumed");
}
function paintCommon(s){
  document.getElementById("gbusy").textContent =
    s.busy ? "- playing" : "";
  document.getElementById("pz").textContent = s.paused ? "resume" : "pause";
  document.body.classList.toggle("paused", s.paused);
  document.getElementById("events").textContent =
    (s.events || []).join("\\n") || "nothing yet";
}
async function refresh(){
  const s = await api("/state");
  J = s.joints;
  const taught = s.taught;
  document.getElementById("joints").innerHTML = J.map(row).join("");
  document.getElementById("poses").innerHTML =
    s.wanted.map(p => poseRow(p, taught)).join("");
  document.getElementById("gestures").innerHTML = s.gestures.map(g =>
    `<button onclick="playGesture('${g}')">${g}</button>`).join("");
  document.getElementById("sub").textContent =
    (s.backend === "serial" ? "- live, moving the real arm"
                            : "- simulator, the arm is not moving") +
    "  \\u00b7 " + s.achievable.toFixed(0) + " deg/s measured";
  document.getElementById("build").textContent =
    "build " + s.build + (s.reopens ? "  reconnects: " + s.reopens : "");
  showLink(s);
  tick(s.ends_in);
}
function showLink(s){
  // A dead serial link looks exactly like working software from here: the page
  // responds, the sliders move, and the arm does not. Say so plainly.
  const el = document.getElementById("link");
  const bad = s.link !== "ok";
  el.classList.toggle("show", bad);
  if (bad) el.textContent = "ARM NOT RESPONDING - " + s.link +
    " (reconnecting automatically; the sliders are not reaching the servos)";
}
function tick(left){
  document.getElementById("dl").textContent =
    "auto-stops in " + Math.floor(left/60) + "m " +
    String(Math.floor(left%60)).padStart(2,"0") + "s";
}
// Only the numbers refresh on a timer. Re-rendering the sliders underneath a
// finger being dragged is exactly how the old page felt unusable.
setInterval(async () => {
  const s = await api("/state");
  for (const j of s.joints){
    const v = document.getElementById("v-" + j.name);
    if (v && !(j.name in pending))
      v.textContent = j.deg.toFixed(1) + "\\u00b0 \\u00b7 " + j.count;
  }
  showLink(s);
  paintCommon(s);
  tick(s.ends_in);
}, 700);
refresh();
</script>
</body></html>
"""

BUILD = hashlib.md5(PAGE).hexdigest()[:7]
PAGE_OUT = PAGE.replace(b"__BUILD__", BUILD.encode())


def make_handler(st):
    arm, latest, deadline, fps = st["arm"], st["latest"], st["deadline"], st["fps"]
    stop_event = st["stop"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def _state(self):
            cfg = arm.config
            sent = arm.sent
            return {
                "backend": arm.backend.name,
                "link": arm.link_status(),
                "reopens": getattr(arm.backend, "reopens", 0),
                "build": BUILD,
                "ends_in": max(0.0, deadline - time.time()),
                "joints": [{"name": j, "deg": sent[j],
                            "count": round(cfg.deg_to_count(j, sent[j])),
                            "lo": cfg.soft_limits(j)[0],
                            "hi": cfg.soft_limits(j)[1],
                            "driven": cfg.is_driven(j)}
                           for j in poses.JOINTS],
                "wanted": [{"name": n, "why": w} for n, w in WANTED],
                "taught": load_taught(),
                "gestures": sorted(gestures.GESTURES),
                "busy": arm.busy(),
                "paused": arm.paused.is_set(),
                "achievable": cfg.achievable,
                "events": list(reversed(arm.events[-14:])),
            }

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PAGE_OUT)))
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.end_headers()
                self.wfile.write(PAGE_OUT)
            elif self.path == "/state":
                self._json(self._state())
            elif self.path == "/stream.mjpg":
                self.stream()
            else:
                self.send_error(404)

        def do_POST(self):
            body = self._body()
            if self.path == "/state":
                self._json(self._state())
            elif self.path == "/set":
                joint = (body.get("joint") or "").strip()
                if joint not in poses.JOINTS:
                    return self._json({"error": "unknown joint"}, 400)
                try:
                    deg = float(body.get("deg"))
                except (TypeError, ValueError):
                    return self._json({"error": "deg must be a number"}, 400)
                self._json({"ok": True, "joint": joint,
                            "deg": arm.set(joint, deg)})
            elif self.path == "/save":
                name = (body.get("name") or "").strip()
                if not name:
                    return self._json({"error": "need a name"}, 400)
                sent = arm.sent
                entry = {j: round(sent[j], 1) for j in poses.JOINTS}
                entry["counts"] = {j: round(arm.config.deg_to_count(j, sent[j]))
                                   for j in poses.JOINTS}
                entry["saved_at"] = datetime.now().isoformat(timespec="seconds")
                data = load_taught()
                data[name] = entry
                save_taught(data)
                log(f"saved {name}: " +
                    " ".join(f"{j}={entry[j]}" for j in poses.JOINTS))
                self._json({"ok": True, "name": name, "pose": entry})
            elif self.path == "/recall":
                name = (body.get("name") or "").strip()
                data = load_taught()
                if name not in data:
                    return self._json({"error": "not taught"}, 404)
                arm.set_pose({j: data[name][j] for j in poses.JOINTS})
                log(f"recalled {name}")
                self._json({"ok": True})
            elif self.path == "/gesture":
                name = (body.get("name") or "").strip()
                if name not in gestures.GESTURES:
                    return self._json({"error": "unknown gesture"}, 400)
                move = arm.play(name)
                self._json({"ok": True, "name": name,
                            "duration": round(move.duration, 2)})
            elif self.path == "/cancel":
                arm.engine.clear()
                arm.note("gesture cancelled")
                self._json({"ok": True})
            elif self.path == "/pause":
                want = bool(body.get("on", not arm.paused.is_set()))
                arm.paused.set() if want else arm.paused.clear()
                arm.note("paused" if want else "resumed")
                self._json({"ok": True, "paused": want})
            elif self.path == "/selftest":
                self._json({"ok": True, "lines": run_selftest(arm, latest)})
            elif self.path == "/stop":
                self._json({"ok": True})
                stop_event.set()
                latest.stop.set()
            else:
                self.send_error(404)

        def stream(self):
            log(f"viewer from {self.client_address[0]}")
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

    return Handler


def run_selftest(arm, latest, swing=8.0, settle=1.6):
    """Move each joint a little and report whether the picture changed.

    "The wrist is not moving" needs to be a measurement, not an argument. The
    camera is on the arm, so any real motion changes the picture; a still scene
    differs by about one grey level and a real move by tens.
    """
    import cv2
    import numpy as np

    def frame():
        for _ in range(40):
            jpeg, _ = latest.get_after(-1, timeout=2.0)
            if jpeg:
                g = cv2.imdecode(np.frombuffer(jpeg, np.uint8),
                                 cv2.IMREAD_GRAYSCALE)
                if g is not None:
                    return cv2.resize(g, (320, 180))
        return None

    out = []
    start = arm.snapshot()
    for j in poses.JOINTS:
        if not arm.config.is_driven(j):
            out.append(f"{j}: pinned")
            continue
        lo, hi = arm.config.soft_limits(j)
        here = arm.snapshot()[j]
        # Swing whichever way there is room for, so a joint near a limit is
        # still tested rather than reported dead.
        to = here + swing if here + swing <= hi else here - swing
        a = frame()
        arm.set(j, to)
        time.sleep(settle)
        b = frame()
        arm.set(j, here)
        time.sleep(settle)
        if a is None or b is None:
            out.append(f"{j}: no picture")
            continue
        d = float(cv2.absdiff(a, b).mean())
        out.append(f"{j}: {'MOVED' if d > 3.0 else 'NO MOTION'} ({d:.1f})")
        log(f"selftest {out[-1]}")
    arm.set_pose(start)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--camera", type=int, default=settings.CAMERA_INDEX)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--backend", default="serial",
                    choices=("sim", "dry-run", "serial"))
    ap.add_argument("--port-name", default="COM5")
    # Aiming a camera needs detail, not frame rate. teleop's defaults are the
    # other way round on purpose.
    ap.add_argument("--fps", type=float, default=7.0)
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--width", type=int, default=0, help="0 keeps native 1280")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--bind", default="")
    args = ap.parse_args()

    host = args.bind or tailscale_ip()
    if not host:
        log("no Tailscale address to bind to; refusing 0.0.0.0, which would "
            "put the camera on the whole LAN")
        return 2

    config = poses.load()
    if args.backend == "serial":
        backend = backends.SerialBackend(args.port_name, config).open()
        state = backend.read_state()
        start = state[0] if state else poses.get_pose("HOME")
        if state:
            log("arm reports " +
                " ".join(f"{j}={start[j]:.1f}" for j in poses.JOINTS))
    elif args.backend == "dry-run":
        backend, start = backends.DryRunBackend(), poses.get_pose("HOME")
    else:
        backend, start = backends.SimBackend(config), poses.get_pose("HOME")

    arm = Arm(config, backend, start=start)
    deadline = time.time() + args.minutes * 60.0
    latest = Latest()
    stop_event = threading.Event()

    threading.Thread(target=grabber, args=(latest, args, deadline),
                     daemon=True).start()
    threading.Thread(target=arm.run, daemon=True).start()

    srv = ThreadingHTTPServer((host, args.port), make_handler({
        "arm": arm, "latest": latest, "deadline": deadline,
        "fps": args.fps, "stop": stop_event}))
    srv.daemon_threads = True
    log(f"backend={backend.name} page build {BUILD}")
    log(f"serving http://{host}:{args.port}/ (http://msi:{args.port}/) "
        f"for {args.minutes:.0f} min")
    try:
        while not stop_event.is_set() and time.time() < deadline:
            srv.handle_request()
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        stop_event.set()
        arm.stop.set()
        latest.stop.set()
        srv.server_close()
        time.sleep(0.3)
        backend.close()
        log(f"stopped. taught poses are in {TAUGHT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
