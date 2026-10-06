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
from avi_bot.personality import (attention as att_mod,            # noqa: E402
                                 behaviors, mood as mood_mod)
from avi_bot import env as env_mod                                # noqa: E402
from avi_bot.perception import parallax                           # noqa: E402
from avi_bot.perception import roommap as roommap_mod             # noqa: E402

# Keys from .env, once, at import. The dashboard is what runs on the MSI, so
# if this does not happen here the cloud route is quietly unavailable there
# while testing fine from the Mac, where the key was exported by hand.
_LOADED_KEYS = env_mod.load()
from avi_bot.voice import beeps as beeps_mod                      # noqa: E402

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
        # Breathing is 2 degrees rather than the default 1.5, and it moves the
        # wrist as well as the shoulder, because the camera is on the wrist:
        # a bob there is visible in the feed, which is how this gets reviewed.
        self.engine.breathing.amp = 2.0
        self.mood = mood_mod.Mood()
        self.selector = behaviors.BehaviourSelector()
        self.attention = att_mod.Attention(config)
        self.idle_mode = threading.Event()
        self.tracking = threading.Event()
        self.last_behaviour = None
        self.detector = None
        self.last_person = None
        # Confidence alone cannot separate Shaurya (0.75-0.89) from the arm's
        # own hardware in shot (0.56-0.72): at 0.70 the tracker locked onto
        # itself, at 0.75 it found nothing in 84 frames. Parallax separates the
        # same two things by a factor of thirty, so the floor can come back
        # down and geometry can do the rejecting. See avi_bot/perception.
        self.rejector = parallax.ParallaxTracker(
            float(config.camera["yaw_deg_per_frame"]))
        # Track a named desk object instead of a person. The whole look-at
        # loop -- detector, attention, steering, spring -- is identical; only
        # the choice of target changes. That makes closed-loop convergence
        # testable when nobody is in the room, which is most of the time, and
        # against a landmark that cannot wander off on its own.
        self.track_label = None
        self.last_person = None
        self.track_error = None
        # Counted rather than inferred. Polling /state samples only the most
        # recent attempt, so a slow poll makes a healthy detector look like it
        # is failing most frames -- which is exactly the wrong conclusion to
        # draw before changing a confidence floor.
        self.track_hits = 0
        self.track_misses = 0
        self.last_trust = None
        self.last_light = None
        # Latent logging lives HERE rather than in its own process, because
        # the camera serves one consumer at a time and the two jobs were
        # taking turns: checking on the robot meant pausing the learning, and
        # collecting meant no POV page. Measured 2026-10-06 with YOLO already
        # resident: 5.38 GB of 6.44 free, and V-JEPA 2 ViT-L needs 0.74, so
        # they fit together and the turn-taking was never necessary.
        self.latents = threading.Event()
        self.latent_encoder = None
        self.latent_db = None
        self.latent_count = 0
        self.latent_ms = None
        self.latent_error = None
        # Where things are, as directions the arm can be commanded to. AVI is
        # bolted to a desk and cannot walk, so it never needs metres -- it
        # needs to know which way to turn. Loaded from disk so a survey is
        # worth doing once rather than every restart.
        self.roommap = roommap_mod.RoomMap(
            float(config.camera["yaw_deg_per_frame"]),
            float(config.camera["wrist_deg_per_frame"]))
        self.load_roommap()
        # Live-tunable, so a value can be changed and watched without a push
        # and a restart. Chasing a confidence floor through a deploy cycle each
        # time is the slow way to find it.
        # min_conf is now the DETECTOR's floor and should be low, so parallax
        # gets to see the candidates it needs to judge. trust_conf is the
        # separate gate for candidates parallax could not judge, which is the
        # common case because the arm is usually holding still.
        self.tune = {"min_conf": 0.35, "gain": att_mod.GAIN,
                     "period": 0.4, "deadband": att_mod.DEADBAND_FRAC,
                     "trust_conf": 0.0}
        # Off while poses are being set -- motion nobody asked for makes a
        # slider impossible to land -- and ON while idling, where they are the
        # whole difference between a robot that is alive and one that is
        # switched off. BUILD_SPEC 6.2 calls them continuous secondary layers;
        # leaving them at zero in idle mode was why the first idle run read as
        # "kinda just standing there".
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

    def play(self, name, why=""):
        move = gestures.make(name)
        self.engine.push(move)
        # Mood changes how a move reads, not which move it is (BUILD_SPEC 5.5).
        emotion = self.mood.emotion()
        self.engine.style = engine.Style(**mood_mod.style_for(emotion))
        self.note(f"gesture {name}{why} [{emotion}]")
        return move

    def mood_event(self, name, target=None):
        self.mood.event(name, target=target)
        self.note(f"event {name}"
                  + (f" ({target})" if target else "")
                  + f" -> {self.mood.emotion()}")

    def track_loop(self, latest, deadline, period=0.4):
        """Offer person detections to attention, while tracking is on.

        Its own thread, at a few hertz, because a detection takes tens of
        milliseconds on the GPU and the arm loop runs at 50 Hz: doing it inline
        would stall the thing that has to keep sending P frames inside the
        firmware's 500 ms watchdog.

        The detector is loaded lazily, on first use, so the dashboard still
        starts in a second and still works on a machine with no GPU.
        """
        import cv2
        import numpy as np
        while not self.stop.is_set() and time.time() < deadline:
            if not self.tracking.is_set():
                time.sleep(0.2)
                continue
            try:
                if self.detector is None:
                    self.note("loading YOLO-World for person tracking ...")
                    from detector import DeskDetector
                    self.detector = DeskDetector()
                    self.note("tracking: looking for you")
                jpeg, _ = latest.get_after(-1, timeout=1.5)
                if not jpeg:
                    continue
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8),
                                     cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                near = self.last_person["cx"] if self.last_person else None
                h, w = frame.shape[:2]
                yaw = self.snapshot()["base_yaw"]
                # Every candidate, not just the winner: the one that loses on
                # confidence this frame is exactly the one whose stillness has
                # to be watched across the next pan.
                if self.track_label:
                    found = [d for d in self.detector.everything(
                                 frame, min_conf=float(self.tune["min_conf"]))
                             if d["label"] == self.track_label]
                    found.sort(key=lambda d: -d["conf"])
                    found = [{"box": None, "conf": d["conf"],
                              "cx": float(d["cx"]), "cy": float(d["cy"])}
                             for d in found[:1]]
                else:
                    found = self.detector.people(
                        frame, min_conf=float(self.tune["min_conf"]))
                if self.track_label:
                    # No parallax test on a deliberately chosen landmark. It
                    # is a real object in the room, so it moves with the pan
                    # by construction, and running the filter here would only
                    # test the filter against itself.
                    live = found
                else:
                    self.rejector.classify(found, yaw, w)
                    # Brightness-aware, because a fixed threshold is wrong
                    # somewhere: tuned for the dark it misses him in daylight,
                    # tuned for daylight it hallucinates him at night.
                    # tune["trust_conf"] is a manual override when > 0.
                    manual = float(self.tune.get("trust_conf") or 0.0)
                    lit = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()
                    trust = manual or parallax.trust_for_light(lit)
                    self.last_trust = round(trust, 2)
                    self.last_light = round(float(lit), 1)
                    live = [c for c in found
                            if self.rejector.accept(c, min_conf_unknown=trust)]
                p = None
                for c in live:
                    # Continuity is real evidence -- a person does not
                    # teleport -- so it breaks ties between similar scores.
                    c["score"] = c["conf"]
                    if near is not None and w:
                        c["score"] += 0.10 * (1.0 - min(
                            1.0, abs(c["cx"] - near) / (w / 2.0)))
                    if p is None or c["score"] > p["score"]:
                        p = c
                if p:
                    self.track_hits += 1
                    self.last_person = {"conf": round(p["conf"], 2),
                                        "cx": round(p["cx"]),
                                        "cy": round(p.get("cy", 0)),
                                        "w": w, "h": h}
                    self.attention.offer("face", p["cx"], p["cy"], w, h,
                                         label="person", conf=p["conf"])
                else:
                    self.track_misses += 1
                    self.last_person = None
                    self.attention.drop("face")
            except Exception as exc:
                self.note(f"tracking error (continuing): {exc!r}")
                time.sleep(1.0)
            time.sleep(period)

    # The map is something AVI learned about the room, so it follows the same
    # launch-day rule as the latents: dev work accumulates in its own file and
    # launch day genuinely starts from nothing. Discovering his room is part of
    # what he wants to watch happen, not something to restore from a backup.
    ROOMMAP_PROFILES = {"dev": "roommap-dev.json", "live": "roommap.json"}

    @classmethod
    def roommap_path(cls, profile=None):
        profile = profile or os.environ.get("AVI_PROFILE", "dev")
        name = cls.ROOMMAP_PROFILES.get(profile)
        if name is None:
            raise ValueError(
                f"unknown profile {profile!r}; "
                f"have {sorted(cls.ROOMMAP_PROFILES)}")
        return os.path.join(os.environ.get("USERPROFILE", "."),
                            "avi-remote", name)

    @property
    def ROOMMAP_PATH(self):
        return self.roommap_path()

    def load_roommap(self):
        """Load the saved map. A missing file is normal; a broken one is not.

        The two were one bare `except: pass`, which made "nothing learned yet"
        and "the map on disk no longer parses" look identical from the
        dashboard -- so a schema change could quietly empty the map and the
        only symptom would be a robot that had forgotten the room.
        """
        path = self.ROOMMAP_PATH
        if not os.path.exists(path):
            return False          # no map yet is the normal first case
        try:
            with open(path) as f:
                self.roommap = roommap_mod.RoomMap.from_dict(json.load(f))
            return True
        except Exception as exc:
            self.note(f"room map at {path} did not load, starting empty: {exc!r}")
            return False

    def save_roommap(self):
        try:
            os.makedirs(os.path.dirname(self.ROOMMAP_PATH), exist_ok=True)
            with open(self.ROOMMAP_PATH, "w") as f:
                json.dump(self.roommap.to_dict(), f, indent=1)
            return True
        except Exception as exc:
            self.note(f"could not save the room map: {exc!r}")
            return False

    def latent_loop(self, latest, deadline, period=15.0):
        """One V-JEPA latent every `period` seconds, while enabled.

        Its own thread for the same reason the detector has one: the encoder
        takes a few hundred milliseconds and must never delay a P frame inside
        the firmware's watchdog.

        Stores VECTORS, never frames. A failure here disables the loop and
        records why rather than retrying every period -- a missing model would
        otherwise fill the log and the GPU with the same error forever.
        """
        import cv2
        import numpy as np
        from datetime import datetime
        while not self.stop.is_set() and time.time() < deadline:
            if not self.latents.is_set() or self.latent_error:
                time.sleep(1.0)
                continue
            started = time.time()
            try:
                if self.latent_encoder is None:
                    self.note("loading V-JEPA 2 for latent logging ...")
                    import latent_log
                    self.latent_db = latent_log.open_db(
                        latent_log.PROFILES["dev"])
                    self.latent_encoder = latent_log.Encoder()
                    self.note("latents: recording")
                # A clip from the stream the dashboard already has, rather
                # than a second camera handle -- there is only one camera.
                frames, seq = [], -1
                gray = None
                for _ in range(latent_log.CLIP_FRAMES):
                    jpeg, seq = latest.get_after(seq, timeout=2.0)
                    if not jpeg:
                        break
                    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8),
                                       cv2.IMREAD_COLOR)
                    if bgr is None:
                        break
                    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                    frames.append(cv2.resize(
                        cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB),
                        (latent_log.CLIP_SIZE, latent_log.CLIP_SIZE),
                        interpolation=cv2.INTER_AREA))
                if len(frames) < latent_log.CLIP_FRAMES or gray is None:
                    time.sleep(1.0)
                    continue
                vec, ms = self.latent_encoder(frames)
                now = time.time()
                lt = datetime.fromtimestamp(now)
                self.latent_db.execute(
                    "INSERT INTO latents (ts, local_time, hour, weekday,"
                    " changed, light_mean, light_sd, encode_ms, model, pool,"
                    " dim, vec) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, lt.strftime("%Y-%m-%d %H:%M:%S"), lt.hour,
                     lt.weekday(), 0, round(float(gray.mean()), 1),
                     round(float(gray.std()), 1), round(ms, 1),
                     self.latent_encoder.model_id,
                     self.latent_encoder.pool, int(vec.shape[0]),
                     vec.astype("float16").tobytes()))
                self.latent_db.commit()
                self.latent_count += 1
                self.latent_ms = round(ms, 1)
            except Exception as exc:
                self.latent_error = f"{type(exc).__name__}: {exc}"[:160]
                self.note(f"latents off: {self.latent_error}")
            left = period - (time.time() - started)
            if left > 0:
                time.sleep(left)

    def follow(self):
        """One steering step. Called from the arm loop."""
        if not self.tracking.is_set():
            self.engine.look_at.weight = 0.0
            self.track_error = None
            return
        target = self.attention.choose(frame=(1280, 720))
        if target is None or target.source == "idle":
            # Nothing real to look at: let the spring relax rather than
            # chasing an invented point, which would fight the idle saccades.
            self.engine.look_at.set_target(yaw=0.0, pitch=0.0)
            self.engine.look_at.weight = 0.4
            self.track_error = None
            return
        self.attention.steer(self.engine.look_at, target, weight=1.0)
        ex, ey = self.attention.error_degrees(target)
        self.track_error = (round(ex, 1), round(ey, 1))

    def set_alive(self, on):
        """Breathing and saccades: the layers that make stillness read as calm
        rather than as powered down."""
        self.engine.breathing.weight = 1.0 if on else 0.0
        self.engine.saccades.weight = 1.0 if on else 0.0

    def tune_alive(self):
        """Scale the idle layers by mood, per BUILD_SPEC 5.5.

        Arousal drives how often the eyes flick and how deep the breath is; a
        sleepy robot breathes slowly and deeply and barely glances around, an
        excited one does the opposite. This is the part that makes stillness
        itself expressive, which matters a great deal on an arm too slow for
        big gestures.
        """
        m = self.mood
        self.engine.breathing.freq = 0.10 + 0.10 * m.arousal
        self.engine.breathing.depth = 1.6 - 0.6 * m.arousal
        self.engine.saccades.sigma = 0.8 + 2.2 * m.arousal
        self.engine.saccades.min_gap = 3.5 - 2.5 * m.arousal
        self.engine.saccades.max_gap = 7.0 - 3.5 * m.arousal
        if m.emotion() == "sleepy":
            self.engine.breathing.depth *= gestures.DOZE_BREATH

    def run_idle(self, now):
        """One step of BUILD_SPEC 5.4's idle policy.

        Only ever starts something while the arm is free. A behaviour that
        interrupted a running gesture would make the robot look indecisive
        rather than alive.
        """
        if not self.idle_mode.is_set():
            return
        self.tune_alive()
        if self.engine.busy():
            return
        if not self.selector.due(now):
            return
        hour = time.localtime(now).tm_hour
        quiet = beeps_mod.is_quiet_hours(hour)
        pick = self.selector.choose(self.mood, now, quiet_hours=quiet)
        self.last_behaviour = pick
        gesture = self.selector.gesture_for(pick)
        chirp = beeps_mod.for_emotion(self.mood.emotion())
        note = f"idle: {pick}"
        if chirp and beeps_mod.should_play(chirp, hour):
            # No speaker is wired up yet, so the chirp is reported rather than
            # played. Saying which one WOULD play is what makes the policy
            # reviewable before any audio hardware exists.
            note += f" (would chirp {chirp})"
        if gesture:
            self.play(gesture, why=f" <- {pick}")
        else:
            self.note(note)

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
                    time.sleep(float(self.tune["period"]))
                    continue
                self.mood.tick(now)
                self.follow()
                self.run_idle(now)
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


POV = b"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AVI POV</title>
<style>
  :root { --bg:#0e1116; --fg:#e6e8eb; --dim:#8b949e; --ok:#3fb950;
          --warn:#d29922; --bad:#f85149; }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
  header { display:flex; align-items:baseline; gap:12px; padding:10px 16px;
           border-bottom:1px solid #222831 }
  h1 { font-size:15px; margin:0; letter-spacing:.04em }
  #age { color:var(--dim); font-size:12px }
  main { padding:16px; max-width:900px; margin:0 auto }
  .shot { width:100%; border-radius:8px; background:#000; display:block;
          aspect-ratio:16/9; object-fit:contain }
  .grid { display:grid; gap:8px; margin-top:14px;
          grid-template-columns:repeat(auto-fit,minmax(130px,1fr)) }
  .cell { background:#161b22; border:1px solid #222831; border-radius:6px;
          padding:8px 10px }
  .k { color:var(--dim); font-size:11px; text-transform:uppercase;
       letter-spacing:.06em }
  .v { font-size:16px; margin-top:2px }
  .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
  #joints { margin-top:14px; font-size:13px; color:var(--dim) }
  #joints b { color:var(--fg); font-weight:500 }
  footer { padding:10px 16px; color:var(--dim); font-size:12px }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) { --bg:#f6f8fa; --fg:#1f2328;
      --dim:#59636e; }
    :root:not([data-theme="dark"]) .cell { background:#fff;
      border-color:#d1d9e0 }
    :root:not([data-theme="dark"]) header { border-color:#d1d9e0 }
  }
</style></head><body>
<header><h1>AVI \\u00b7 POV</h1><span id="age">connecting\\u2026</span></header>
<main>
  <img class="shot" id="shot" alt="camera view">
  <div class="grid" id="grid"></div>
  <div id="joints"></div>
</main>
<footer>live from the MSI over the tailnet. no frame is stored.</footer>
<script>
var shot = document.getElementById("shot");
shot.src = "/stream.mjpg";
function cell(k, v, cls) {
  return '<div class="cell"><div class="k">' + k + '</div><div class="v ' +
         (cls || "") + '">' + v + '</div></div>';
}
function light(n) {
  if (n === null || n === undefined) return ["?", "warn"];
  if (n < 30) return [n + " dark", "warn"];
  return [n + "", "ok"];
}
function tick() {
  fetch("/state", {cache: "no-store"}).then(function (r) { return r.json(); })
  .then(function (s) {
    document.getElementById("age").textContent =
      "build " + s.build + " \\u00b7 updated " + new Date().toLocaleTimeString();
    var hits = s.track_hits || 0, miss = s.track_misses || 0;
    var rate = (hits + miss) ? Math.round(100 * hits / (hits + miss)) : null;
    var lit = light(s.light);
    var g = "";
    g += cell("tracking", s.tracking ? "on" : "off",
              s.tracking ? "ok" : "dim");
    g += cell("sees you", s.person ? ("yes " + s.person.conf) : "no",
              s.person ? "ok" : "");
    g += cell("hit rate", rate === null ? "\\u2014" : rate + "%",
              rate === null ? "" : (rate > 50 ? "ok" : "warn"));
    g += cell("light", lit[0], lit[1]);
    g += cell("trust floor", s.trust === null || s.trust === undefined
              ? "\\u2014" : s.trust, "");
    g += cell("error", s.track_error ? (s.track_error[0] + "\\u00b0") : "\\u2014",
              s.track_error && Math.abs(s.track_error[0]) < 5 ? "ok" : "");
    g += cell("mood", (s.mood && s.mood.emotion) || "\\u2014", "");
    g += cell("busy", s.busy ? "moving" : "still", "");
    g += cell("latents", s.latent_error ? "error"
              : (s.latents ? (s.latent_count + " rec") : "off"),
              s.latent_error ? "bad" : (s.latents ? "ok" : ""));
    document.getElementById("grid").innerHTML = g;
    document.getElementById("joints").innerHTML = (s.joints || []).map(
      function (j) { return j.name + " <b>" + j.deg.toFixed(1) +
                     "\\u00b0</b>"; }).join(" \\u00b7 ");
  })
  .catch(function () {
    document.getElementById("age").textContent = "no answer from the robot";
  });
}
tick();
setInterval(tick, 2000);
</script>
</body></html>
"""

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
 .mv{display:flex;align-items:center;gap:8px;margin:3px 0;
     font:11px ui-monospace,monospace;color:#a1a1aa}
 .mv .nm{width:62px}
 .mv .tr{flex:1;height:8px;background:#27272a;border-radius:4px;position:relative}
 .mv .fl{position:absolute;top:0;bottom:0;border-radius:4px;background:#60a5fa}
 .mv .zero{position:absolute;top:-2px;bottom:-2px;width:1px;background:#52525b}
 .mv .n{width:46px;text-align:right}
 #emo{color:#fbbf24;text-transform:none;letter-spacing:0;font-weight:600}
 #trkinfo{font:11px ui-monospace,monospace;color:#71717a;margin-top:4px}
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
      <button id="idle" onclick="toggleIdle()">idle behaviour</button>
      <button id="trk" onclick="toggleTracking()">look at me</button>
      <code id="dl"></code>
    </div>
    <div id="msg"></div>
    <div id="trkinfo"></div>
  </div>
  <div class="side">
    <div id="joints"></div>
    <div class="sec">mood <span id="emo"></span></div>
   <div id="mood"></div>
   <div class="g" id="moodbtns"></div>
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
const MOOD_RANGE = {valence: [-1, 1], arousal: [0, 1],
                    social: [0, 1], energy: [0, 1]};
function paintMood(s){
  const m = s.mood;
  document.getElementById("emo").textContent =
    "- " + m.emotion + (m.sulking ? " (sulking)" : "") +
    (m.focus ? " / focus" : "") + (m.present ? "" : " / away");
  document.getElementById("mood").innerHTML =
    Object.keys(MOOD_RANGE).map(k => {
      const [lo, hi] = MOOD_RANGE[k];
      const f = Math.max(0, Math.min(1, (m[k] - lo) / (hi - lo)));
      const z = (0 - lo) / (hi - lo);
      const left = Math.min(f, z), right = Math.max(f, z);
      return `<div class="mv"><span class="nm">${k}</span>`+
        `<span class="tr"><span class="zero" style="left:${z*100}%"></span>`+
        `<span class="fl" style="left:${left*100}%;width:${(right-left)*100}%">`+
        `</span></span><span class="n">${m[k].toFixed(2)}</span></div>`;
    }).join("");
  document.getElementById("idle").textContent =
    s.idle ? "stop idling" : "idle behaviour";
  document.getElementById("idle").className = s.idle ? "pri" : "";
  const tb = document.getElementById("trk");
  tb.textContent = s.tracking ? "stop looking" : "look at me";
  tb.className = s.tracking ? "pri" : "";
  document.getElementById("trkinfo").textContent = !s.tracking ? "" :
    (s.person
      ? "person at x=" + s.person.cx + "/" + s.person.w +
        " conf " + s.person.conf +
        (s.track_error ? "   error " + s.track_error[0] + "\\u00b0, "
                       + s.track_error[1] + "\\u00b0" : "")
      : "looking for you ...");
}
async function moodEvent(n){
  const body = {name: n};
  if (n === "new_object") body.target = "something new";
  await api("/mood_event", body);
  msg("event: " + n);
}
async function toggleTracking(){
  const r = await api("/tracking", {});
  msg(r.tracking ? "tracking on - loading the detector the first time, "
                 + "which takes a few seconds"
                 : "tracking off");
}
async function toggleIdle(){
  const r = await api("/idle", {});
  msg(r.idle ? "idling: it will pick its own behaviours"
             : "idle behaviour off");
}
function paintCommon(s){
  document.getElementById("gbusy").textContent =
    s.busy ? "- playing" : "";
  document.getElementById("pz").textContent = s.paused ? "resume" : "pause";
  document.body.classList.toggle("paused", s.paused);
  paintMood(s);
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
  document.getElementById("moodbtns").innerHTML = s.mood_events.map(e =>
    `<button onclick="moodEvent('${e}')">${e}</button>`).join("");
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
POV_OUT = POV.replace(b"__BUILD__", BUILD.encode())


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
                "mood": arm.mood.snapshot(),
                "idle": arm.idle_mode.is_set(),
                "behaviour": arm.last_behaviour,
                "tracking": arm.tracking.is_set(),
                "person": arm.last_person,
                "track_error": arm.track_error,
                "track_hits": arm.track_hits,
                "track_misses": arm.track_misses,
                "tune": arm.tune,
                "attached": [round(k.cx)
                             for k in arm.rejector.attached_tracks()],
                "parallax_why": arm.rejector.describe(),
                "parallax_rejected": arm.rejector.rejected,
                "parallax_accepted": arm.rejector.accepted,
                "trust": arm.last_trust,
                "light": arm.last_light,
                "latents": arm.latents.is_set(),
                "latent_count": arm.latent_count,
                "latent_ms": arm.latent_ms,
                "latent_error": arm.latent_error,
                "map_places": len(arm.roommap.known()),
                "map_known": [
                    {"label": p.label, "yaw": round(p.yaw, 1),
                     "wrist": round(p.wrist, 1), "seen": p.count,
                     "conf": round(p.best_conf, 2),
                     "near": arm.roommap.nearest_anchor(p.yaw, p.wrist)[0]}
                    for p, w in arm.roommap.known()[:12]],
                "track_label": arm.track_label,
                "mood_events": ["praise", "scold", "arrived", "left",
                                "task_ok", "task_failed", "new_object",
                                "focus_start", "focus_end"],
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
            elif self.path in ("/pov", "/pov.html"):
                # A deliberately tiny page: the camera's view plus the few
                # numbers that say whether the robot is alive. Exists because
                # the full dashboard is a control surface -- forty controls
                # and a mood panel -- and checking on the robot from a phone
                # at school is a different job from driving it.
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(POV_OUT)))
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(POV_OUT)
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
            elif self.path == "/mood_event":
                name = (body.get("name") or "").strip()
                try:
                    arm.mood_event(name, target=body.get("target"))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                self._json({"ok": True, "mood": arm.mood.snapshot()})
            elif self.path == "/idle":
                want = bool(body.get("on", not arm.idle_mode.is_set()))
                arm.idle_mode.set() if want else arm.idle_mode.clear()
                arm.set_alive(want)
                arm.note("idle behaviour on, breathing" if want
                         else "idle behaviour off, holding still")
                self._json({"ok": True, "idle": want})
            elif self.path == "/tune":
                for k, v in (body or {}).items():
                    if k not in arm.tune:
                        return self._json({"error": f"unknown knob {k}; "
                                           f"have {sorted(arm.tune)}"}, 400)
                    arm.tune[k] = float(v)
                arm.attention.gain = float(arm.tune["gain"])
                att_mod.DEADBAND_FRAC = float(arm.tune["deadband"])
                arm.note("tuned " + " ".join(f"{k}={arm.tune[k]}"
                                             for k in sorted(body or {})))
                self._json({"ok": True, "tune": arm.tune})
            elif self.path == "/reset_counts":
                arm.track_hits = arm.track_misses = 0
                arm.rejector.rejected = arm.rejector.accepted = 0
                arm.rejector.tracks = []
                self._json({"ok": True})
            elif self.path == "/detect_debug":
                floor = float(body.get("min_conf", 0.05))
                self._json(detect_debug(arm, latest, floor))
            elif self.path in ("/probe_parallax", "/probe_static"):
                self._json(probe_parallax(
                    arm, latest, swing=float(body.get("swing", 15.0)),
                    min_conf=float(body.get("min_conf", 0.35)),
                    frames=int(body.get("frames", 3))))
            elif self.path in ("/probe_wrist", "/probe_yaw"):
                wrist = self.path == "/probe_wrist"
                self._json(probe_axis(
                    arm, latest,
                    joint="wrist" if wrist else "base_yaw",
                    axis="cy" if wrist else "cx",
                    step=float(body.get("step", 6.0)),
                    n=int(body.get("n", 5)),
                    min_conf=float(body.get("min_conf", 0.25))))
            elif self.path == "/track_label":
                want = body.get("label") or None
                arm.track_label = str(want) if want else None
                arm.last_person = None
                arm.attention.drop("face")
                arm.note(f"tracking target: {arm.track_label or 'person'}")
                self._json({"ok": True, "track_label": arm.track_label})
            elif self.path == "/survey":
                self._json(run_survey(
                    arm, latest,
                    levels=body.get("wrist_levels"),
                    min_conf=float(body.get("min_conf", 0.30)),
                    pose=body.get("pose", "OBSERVE"),
                    settle=float(body.get("settle", 1.8)),
                    frames=int(body.get("frames", 2))))
            elif self.path == "/forget_map":
                n = len(arm.roommap.places)
                arm.roommap.places = []
                arm.save_roommap()
                self._json({"ok": True, "forgot": n})
            elif self.path == "/latents":
                want = bool(body.get("on", not arm.latents.is_set()))
                arm.latents.set() if want else arm.latents.clear()
                if want:
                    arm.latent_error = None      # let a retry be possible
                arm.note("latents on" if want else "latents off")
                self._json({"ok": True, "latents": want,
                            "count": arm.latent_count})
            elif self.path == "/tracking":
                want = bool(body.get("on", not arm.tracking.is_set()))
                arm.tracking.set() if want else arm.tracking.clear()
                if not want:
                    arm.attention.drop("face")
                arm.note("tracking on" if want else "tracking off")
                self._json({"ok": True, "tracking": want})
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
                    time.sleep(float(self.tune["period"]))
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def _frame(latest):
    import cv2
    import numpy as np
    for _ in range(40):
        jpeg, _ = latest.get_after(-1, timeout=2.0)
        if jpeg:
            f = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if f is not None:
                return f
    return None


def detect_debug(arm, latest, min_conf=0.05):
    """What does the camera ACTUALLY see -- as labels, never as pixels.

    "0 hits in 1686 frames" has two completely different causes: nobody is in
    front of the camera, or the detector is broken. A confidence floor cannot
    tell them apart and neither can staring at the counter. This answers it
    without a frame leaving the MSI, because the camera faces Shaurya's room.
    """
    if arm.detector is None:
        from detector import DeskDetector
        arm.detector = DeskDetector()
    frame = _frame(latest)
    if frame is None:
        return {"error": "no picture"}
    found = arm.detector.everything(frame, min_conf=min_conf)
    h, w = frame.shape[:2]
    # Brightness, because "no detections" and "dark room" are indistinguishable
    # from the counter and we have already confused them once. A number, not a
    # picture: mean level and spread over the whole frame say whether there is
    # anything to see without anything leaving the MSI.
    import cv2
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    lit = {"mean": round(float(g.mean()), 1), "sd": round(float(g.std()), 1)}
    log(f"detect_debug floor={min_conf}: {len(found)} detections, "
        f"mean level {lit['mean']}")
    return {"frame": [w, h], "min_conf": min_conf, "light": lit,
            "dark": lit["mean"] < 30.0,
            "n": len(found), "found": found[:25]}


# Detections this weak are still reported, never recorded. The point is to
# tell "the wall is empty" apart from "the desk was there at 0.22".
REPORT_FLOOR = 0.10


def run_survey(arm, latest, levels=None, min_conf=0.30, settle=1.8,
               frames=2, pose="OBSERVE"):
    """Look around the room on purpose and write down what is where.

    This is the "look around" behaviour with a point to it. The stops come
    from the map itself, stepping by a frame width minus an overlap so nothing
    sitting on a seam is missed, and sweeping boustrophedon so the arm does
    not travel the whole width back between rows.

    Every detection is recorded as a DIRECTION -- the (yaw, wrist) that would
    centre it -- because that is the only thing a desk-bound robot can act on.
    The taught poses are anchored first, so the result can be described in
    Shaurya's own terms rather than in commanded degrees.
    """
    if arm.detector is None:
        from detector import DeskDetector
        arm.detector = DeskDetector()
    taught = load_taught()
    for name, joints in (taught or {}).items():
        if "base_yaw" in joints and "wrist" in joints:
            arm.roommap.anchor(name, joints["base_yaw"], joints["wrist"])

    # Shoulder and elbow are held FIXED for the whole survey, from a pose
    # that faces the desk. They cannot vary: the camera rides three links,
    # so the same (yaw, wrist) aims somewhere else once the shoulder moves,
    # and every sighting would be filed under a direction the robot cannot
    # reproduce. The map records the shape it was built at and refuses to
    # mix, so this is enforced rather than hoped for.
    if pose and taught and pose in taught:
        arm.set("shoulder", taught[pose]["shoulder"])
        arm.set("elbow", taught[pose]["elbow"])
        time.sleep(settle)
    shape = arm.snapshot()

    lo, hi = arm.config.soft_limits("base_yaw")
    wlo, whi = arm.config.soft_limits("wrist")
    if not levels:
        # Three bands around where the taught poses look, clamped to limits.
        levels = [max(wlo, 125.0), 148.0, min(whi, 170.0)]
    levels = [max(wlo, min(whi, float(v))) for v in levels]
    stops = arm.roommap.survey_stops((lo, hi), levels)

    start = arm.snapshot()
    seen, visited = 0, 0
    # What each stop actually saw, including what was thrown away. The first
    # survey reported "2 sightings across 6 stops" and nothing else, which is
    # the same output whether the detector saw an empty wall, saw the desk at
    # 0.25 and lost it all to the floor, or saw only a person. Three different
    # problems, one number, and no way to tune the grid without knowing which.
    per_stop = []
    for yaw, wrist in stops:
        arm.set("base_yaw", yaw)
        arm.set("wrist", wrist)
        time.sleep(settle)
        looked = {"yaw": yaw, "wrist": wrist, "frames": 0,
                  "kept": [], "below_floor": [], "people": 0}
        for _ in range(frames):
            frame = _frame(latest)
            if frame is None:
                continue
            visited += 1
            looked["frames"] += 1
            # Detect well BELOW the recording floor so near-misses are visible.
            # Only what clears min_conf is written to the map.
            for d in arm.detector.everything(
                    frame, min_conf=min(min_conf, REPORT_FLOOR)):
                if d["label"] == "person":
                    # People move. Mapping where a person was is mapping
                    # nothing, and would poison the one thing this map is for.
                    looked["people"] += 1
                    continue
                if d["conf"] < min_conf:
                    looked["below_floor"].append(
                        [d["label"], round(d["conf"], 2)])
                    continue
                looked["kept"].append([d["label"], round(d["conf"], 2)])
                try:
                    arm.roommap.observe(
                        d["label"], d["cx"], d["cy"], yaw, wrist,
                        conf=d["conf"], px_size=max(d["w"], d["h"]),
                        shoulder=shape["shoulder"], elbow=shape["elbow"])
                except roommap_mod.Unreachable as exc:
                    arm.set_pose(start)
                    return {"error": str(exc),
                            "hint": "POST /forget_map to rebuild at this shape"}
                seen += 1
        per_stop.append(looked)
    arm.set_pose(start)
    arm.roommap.forget_stale()
    saved = arm.save_roommap()
    known = arm.roommap.known()
    unconfirmed = [p for p in arm.roommap.places if not p.confirmed]
    near_misses = sum(len(st["below_floor"]) for st in per_stop)
    blank = sum(1 for st in per_stop
                if not st["kept"] and not st["below_floor"] and not st["people"])
    log(f"survey: {len(stops)} stops, {seen} sightings, "
        f"{len(known)} confirmed, {len(unconfirmed)} seen once; "
        f"{blank} stops saw nothing at all, {near_misses} detections lost to "
        f"the {min_conf} floor")
    return {"stops": len(stops), "frames": visited, "sightings": seen,
            "saved": saved, "levels": levels, "min_conf": min_conf,
            "per_stop": per_stop, "blank_stops": blank,
            "near_misses": near_misses, "report_floor": REPORT_FLOOR,
            "shape": {"shoulder": shape["shoulder"], "elbow": shape["elbow"]},
            "unconfirmed": len(unconfirmed),
            "known": [{"label": p.label, "yaw": round(p.yaw, 1),
                       "wrist": round(p.wrist, 1), "seen": p.count,
                       "views": len(p.stops),
                       "conf": round(p.best_conf, 2),
                       "reachable": arm.roommap.reachable(
                           p.yaw, p.wrist, (lo, hi), (wlo, whi)),
                       "near": arm.roommap.nearest_anchor(p.yaw, p.wrist)[0]}
                      for p, w in known]}


def probe_parallax(arm, latest, swing=15.0, settle=1.8, min_conf=0.35,
                   frames=3):
    """Pan on purpose and report what each candidate turns out to be.

    The passive version only learns while the arm happens to be panning, and
    the failure this exists to prevent is exactly the one where the arm has
    saturated and STOPPED panning. So this asks the question deliberately.

    `swing` has a floor set by geometry: the two hypotheses -- moved with the
    pan, or did not move -- have to separate by more than detection jitter
    before a match between them means anything.
    """
    if arm.detector is None:
        from detector import DeskDetector
        arm.detector = DeskDetector()
    need = parallax.MIN_SEPARATION_PX / arm.rejector.px_per_deg(1280)
    if swing < need:
        return {"error": f"swing {swing} deg is too small to tell the two "
                         f"hypotheses apart; need at least {need:.1f}"}
    start = arm.snapshot()
    lo, hi = arm.config.soft_limits("base_yaw")
    here = start["base_yaw"]
    stops = [here, max(lo, here - swing), min(hi, here + swing), here,
             max(lo, here - swing)]
    seen = []
    for yaw in stops:
        arm.set("base_yaw", yaw)
        time.sleep(settle)
        for _ in range(frames):
            frame = _frame(latest)
            if frame is None:
                continue
            w = frame.shape[1]
            found = arm.detector.people(frame, min_conf=min_conf)
            arm.rejector.classify(found, yaw, w)
            for c in found:
                seen.append({"yaw": round(yaw, 1), "cx": round(c["cx"]),
                             "conf": round(c["conf"], 2),
                             "verdict": c["parallax"], "track": c["track"]})
    arm.set_pose(start)
    log(f"probe_parallax: {len(seen)} sightings, "
        f"{len(arm.rejector.attached_tracks())} attached")
    return {"swing": swing, "sightings": seen,
            "attached": [round(k.cx) for k in arm.rejector.attached_tracks()],
            "why": arm.rejector.describe()}


def probe_axis(arm, latest, joint="wrist", axis="cy", step=6.0, n=5,
               min_conf=0.25):
    """Measure wrist_deg_per_frame the way yaw was measured: off a DETECTION.

    `joint` is the joint to step and `axis` the box coordinate that should
    move as a result: ("wrist", "cy") for vertical, ("base_yaw", "cx") for
    horizontal. Both constants in [camera] come out of the same procedure,
    which is the point -- yaw's 29.0 was measured a different way, and a
    constant the parallax filter depends on deserves a second method.

    THE METHOD MATTERS. Four optical measurements of arm motion failed on
    2026-10-05: phase correlation twice (no peak, 0.00-0.07), dense Farneback
    (17-78 px disagreement) and template matching (false matches, 165 px) --
    all while the scene's own noise with the arm still measured 4 px, so the
    methods were at fault, not the signal. Do not attempt a fifth. What DID
    work was watching a detected box move, so that is what this does: pick the
    most confident thing on the desk, step the wrist across a span, and fit its
    cy against the commanded angle.

    A fit is reported only if it is actually linear. A slope from two noisy
    points is how a guess gets written down as a measurement.
    """
    if arm.detector is None:
        from detector import DeskDetector
        arm.detector = DeskDetector()
    start = arm.snapshot()
    lo, hi = arm.config.soft_limits(joint)
    here = start[joint]
    half = step * (n - 1) / 2.0
    angles = [here - half + i * step for i in range(n)]
    angles = [a for a in angles if lo <= a <= hi]
    if len(angles) < 3:
        arm.set_pose(start)
        return {"error": f"no room to sweep: {joint} at {here} in {lo}..{hi}"}

    # Lock onto one landmark. The desk's most confident object, chosen once at
    # the middle of the sweep, then followed by nearest-box so a second mug
    # cannot be mistaken for the first one moving.
    arm.set(joint, angles[len(angles) // 2])
    time.sleep(1.8)
    frame = _frame(latest)
    if frame is None:
        arm.set_pose(start)
        return {"error": "no picture"}
    h, w = frame.shape[:2]
    seed = [d for d in arm.detector.everything(frame, min_conf=min_conf)
            if d["label"] != "person"]
    if not seed:
        arm.set_pose(start)
        return {"error": f"nothing on the desk above {min_conf} to track"}
    label = seed[0]["label"]

    pts, track = [], seed[0]
    for a in angles:
        arm.set(joint, a)
        time.sleep(1.8)
        frame = _frame(latest)
        if frame is None:
            continue
        cands = [d for d in arm.detector.everything(frame, min_conf=min_conf)
                 if d["label"] == label]
        if not cands:
            continue
        track = min(cands, key=lambda d: abs(d["cx"] - track["cx"]))
        pts.append((a, float(track[axis])))
    arm.set_pose(start)

    if len(pts) < 3:
        return {"error": f"only saw {label} in {len(pts)} of {len(angles)} "
                         f"positions", "points": pts}
    # Least squares, plus the residual, because the residual is the only thing
    # that says whether the slope means anything.
    k = len(pts)
    mx = sum(a for a, _ in pts) / k
    my = sum(y for _, y in pts) / k
    sxx = sum((a - mx) ** 2 for a, _ in pts)
    sxy = sum((a - mx) * (y - my) for a, y in pts)
    if sxx == 0:
        return {"error": "all samples at one angle"}
    slope = sxy / sxx                      # px of cy per commanded degree
    syy = sum((y - my) ** 2 for _, y in pts)
    r2 = (sxy * sxy / (sxx * syy)) if syy else 0.0
    span = abs(slope) * (max(a for a, _ in pts) - min(a for a, _ in pts))
    size = h if axis == "cy" else w
    out = {"joint": joint, "axis": axis, "landmark": label,
           "points": [[round(a, 1), round(y)] for a, y in pts],
           "slope_px_per_deg": round(slope, 2), "r2": round(r2, 3),
           "span_px": round(span), "frame": [w, h]}
    # 4 px is the measured scene noise; a span near it proves nothing, and a
    # scattered fit means the landmark was not what moved.
    if span < 40 or r2 < 0.80 or slope == 0:
        out["error"] = ("fit too weak to record: need span >= 40 px and "
                        "r2 >= 0.80")
        return out
    out["deg_per_frame"] = round(abs(size / slope), 1)
    if axis == "cy":
        out["landmark_moves"] = "down" if slope > 0 else "up"
        out["view_tilts"] = "up" if slope > 0 else "down"
    else:
        out["landmark_moves"] = "right" if slope > 0 else "left"
        out["view_pans"] = "left" if slope > 0 else "right"
    log(f"probe_axis {joint}/{axis}: {out['deg_per_frame']} deg/frame "
        f"off {label}, r2={r2:.3f}")
    return out


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
    threading.Thread(target=arm.track_loop, args=(latest, deadline),
                     daemon=True).start()
    threading.Thread(target=arm.latent_loop, args=(latest, deadline),
                     daemon=True).start()

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
