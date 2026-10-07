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
import math
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
from avi_bot.perception import depth as depth_mod                 # noqa: E402
from avi_bot.perception import desk_changes as desk_mod           # noqa: E402
from avi_bot.perception import dome as dome_mod                   # noqa: E402
from avi_bot.perception import horizon as horizon_mod             # noqa: E402
from avi_bot.perception import world as world_mod                 # noqa: E402
from avi_bot.dashboard import settings as settings_mod            # noqa: E402
from avi_bot.dashboard import brain_host as brain_host_mod        # noqa: E402
from avi_bot.perception import spatial as spatial_mod             # noqa: E402
from avi_bot.perception import observations as observations_mod   # noqa: E402
from avi_bot.motion import backlash as backlash_mod               # noqa: E402
from avi_bot.perception import presence as presence_mod           # noqa: E402
from avi_bot.skills import breaks as breaks_mod                   # noqa: E402
from avi_bot.skills import find as find_mod                       # noqa: E402
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
    ("OBSERVE", "spare; NOT the logging pose any more -- it faced a blank "
                "wall (sd 15.5, nothing detected at any base_yaw)"),
    ("ATTENTIVE", "THE LOGGING POSE. Object positions are recorded ONLY from "
                  "here, so aim it at the desk properly"),
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
        if settings_mod.load(settings_mod.path()).get("latents"):
            self.latents.set()          # it was on before the restart
        self.latent_encoder = None
        self.latent_db = None
        self.latent_count = 0
        self.latent_ms = None
        self.latent_error = None
        # Where things are, as directions the arm can be commanded to. AVI is
        # bolted to a desk and cannot walk, so it never needs metres -- it
        # needs to know which way to turn. Loaded from disk so a survey is
        # worth doing once rather than every restart.
        # The deg-per-frame constants come from the MEASURED dome gains when
        # there are any, not from [camera]. The survey caught [camera] out:
        # one laptop was mapped as TWO places 19.6 degrees apart in wrist,
        # which is what a wrong vertical constant does -- it displaces the
        # computed direction by a fraction of a frame, so the same object seen
        # from two elevations lands in two places.
        #
        #     [camera] wrist_deg_per_frame   32.2
        #     dome gain 1.2734 implies       31/1.2734 = 24.3     33% apart
        #
        # Collapsing that duplicate is the test of which is right, and it is a
        # better one than either measurement on its own: the object has to be
        # in one place, so the constant that puts it there is correct.
        self.roommap = roommap_mod.RoomMap(*self.frame_constants(config),
                                           view=world_mod.View.from_config(
                                               config))
        # When he is at the desk, learned by watching. Launch-day rule: the
        # dev store unless AVI_PROFILE=live, same as the map and the latents.
        self.presence = presence_mod.Presence(self.presence_path())
        self.coach = breaks_mod.BreakCoach(self.presence)
        self.desk = desk_mod.DeskLog(self.store_path(desk_mod.PROFILES))
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

    @staticmethod
    def store_path(profiles):
        """A learned store's path under the launch-day rule: dev unless
        AVI_PROFILE=live."""
        profile = os.environ.get("AVI_PROFILE", "dev")
        name = profiles.get(profile)
        if name is None:
            raise ValueError(f"unknown profile {profile!r}")
        d = os.path.join(os.environ.get("USERPROFILE", "."), "avi-remote")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)

    @classmethod
    def presence_path(cls):
        return cls.store_path(presence_mod.PROFILES)

    # The pose that looks at his seat. Absence only counts from here: not
    # seeing him while the camera points at the ceiling is not evidence that
    # he has gone. ATTENTIVE is the pose taught as "looking at where you sit".
    SEAT_VIEW = "ATTENTIVE"
    SEAT_TOLERANCE_DEG = 12.0

    # What a person at the desk looks like to the detector. The false "person"
    # boxes measured on 2026-10-06 were thin strips along the top edge of the
    # frame at high tilt -- one was 347x109, 15% of the frame height. Someone
    # sitting at the desk fills far more than that.
    #
    # And the EMPTY SEAT scores as a person. Measured the same afternoon, with
    # him away, at the seat view: a static box at (979, ~390), 602 x ~660 px --
    # over 90% of the frame -- at confidence 0.38 to 0.39, the same place every
    # look. Almost certainly the chair. At a 0.40 floor it flickered across the
    # line and was counted once within the first minute; two in a row would
    # have been a fake arrival. 0.50 clears the empty seat with margin.
    # STILL TO VALIDATE with him actually sitting there: a seated person should
    # score well above this (0.6+ expected), and if not, this is wrong.
    PERSON_MIN_CONF = 0.50
    PERSON_MIN_HEIGHT = 0.25

    def view_ok_for_presence(self):
        seat = (load_taught() or {}).get(self.SEAT_VIEW)
        if not seat:
            return False
        now = self.snapshot()
        return all(abs(float(now[j]) - float(seat[j])) <= self.SEAT_TOLERANCE_DEG
                   for j in poses.JOINTS if j in seat)

    def presence_loop(self, latest, deadline, period=20.0):
        """Look for him every `period` seconds and learn when he is here.

        Arrivals and departures now fire the mood events that used to need a
        button press, so AVI's mood follows whether he is actually there.
        Stores a yes/no and a confidence per look -- never a frame.
        """
        while not self.stop.is_set() and time.time() < deadline:
            time.sleep(period)
            try:
                frame, _ = _frame(latest)
                if frame is None:
                    continue
                if self.detector is None:
                    from detector import DeskDetector
                    self.detector = DeskDetector()
                h = frame.shape[0]
                people = [d for d in self.detector.everything(
                              frame, min_conf=self.PERSON_MIN_CONF)
                          if d["label"] == "person"
                          and d["h"] >= self.PERSON_MIN_HEIGHT * h]
                conf = max((d["conf"] for d in people), default=None)
                event = self.presence.sample(
                    bool(people), conf=conf,
                    view_ok=self.view_ok_for_presence())
                if event:
                    self.mood_event(event)
                    self.note(f"presence: {event}")
            except Exception as exc:
                self.note(f"presence error (continuing): {exc!r}")
                time.sleep(5.0)

    # The cue for "time for a break". No speakers until the voice module
    # arrives, so it is a gesture: a yawn-and-stretch reads as "let's stretch"
    # without words. Words get added on top when there is something to say
    # them with.
    BREAK_GESTURE = "yawn_wake"

    def pause_loop(self, latest, deadline, period=0.5):
        """Watch for natural pauses and let the coach decide about a break.

        change.py's detector, the one the observer uses, run on the dashboard's
        own stream so it needs no second camera handle. Suppressed whenever
        the arm moves, because the arm's own motion -- including the break
        gesture itself -- would otherwise read as him pausing.
        """
        import change
        det = change.ChangeDetector()
        last = None
        while not self.stop.is_set() and time.time() < deadline:
            time.sleep(period)
            try:
                frame, _ = _frame(latest)
                if frame is None:
                    continue
                now = time.time()
                snap = self.snapshot()
                if last is not None and any(
                        abs(snap[j] - last[j]) > 0.3 for j in snap):
                    det.suppress(now + 2.0)
                last = snap
                if not det.update(frame, now):
                    continue
                if self.coach.on_breakpoint(det.last_reason, now) \
                        == breaks_mod.BREAK:
                    move = self.play(self.BREAK_GESTURE)
                    det.suppress(now + move.duration + 3.0)
                    self.presence.log_event("break_offered", now)
                    self.note(f"break offered after "
                              f"{(now - self.presence.since) / 60:.0f} min "
                              f"at a natural pause")
            except Exception as exc:
                self.note(f"pause loop error (continuing): {exc!r}")
                time.sleep(5.0)

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
    # The voice brain's memory: what he asked, reminders, things he told it.
    BRAIN_PROFILES = {"dev": "brain-dev.db", "live": "brain.db"}

    def observed_notes(self):
        """What AVI has noticed about him, for "what do you know about me"."""
        try:
            _, objects = self.desk.last()
        except Exception:
            objects = []
        placed = [o for o in (objects or []) if "x" in o]
        return observations_mod.notes(self.presence.summary(),
                                      spatial_mod.layout(placed))

    def make_brain_host(self, latest):
        """The voice brain, built on first /ask (brain_host.py)."""
        def recall(name):
            taught = load_taught() or {}
            if name in taught:
                self.set_pose({j: taught[name][j] for j in poses.JOINTS})

        def make():
            from avi_bot import main as main_mod
            body = brain_host_mod.BrainArm(
                self, recall, lambda n, look: find_object(self, n, look=look),
                seat_view=self.SEAT_VIEW)
            return main_mod.Avi(db_path=self.store_path(self.BRAIN_PROFILES),
                                arm=body, mood=self.mood,
                                observed=self.observed_notes)

        return brain_host_mod.Host(
            make, present=lambda: bool(self.presence.here), note=self.note,
            cue=lambda: self.play(brain_host_mod.CUE_GESTURE,
                                  " (announcement)"),
            perform=lambda g: self.play(g, " (reply)"))

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

    FOV_DEG = (52.0, 31.0)          # the camera's own field of view

    @classmethod
    def frame_constants(cls, config):
        """(yaw_deg_per_frame, wrist_deg_per_frame), measured if possible.

        Derived from the dome gains, which were fitted on the arm with large
        steps and a one-sided approach, rather than from [camera], whose
        values came from single-landmark probes that disagree with each other.
        Falls back to [camera] when the dome is unmeasured.
        """
        g = config.dome
        cam = config.camera
        if g.get("measured"):
            fx, fy = cls.FOV_DEG
            try:
                return (fx / float(g["base_yaw"]), fy / float(g["wrist"]))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
        yaw, wrist = cam.get("yaw_deg_per_frame"), cam.get("wrist_deg_per_frame")
        if yaw is None or wrist is None:
            raise RuntimeError(
                "no camera constants: config/joints.toml has neither a "
                "measured [dome] nor a [camera] section, and a guessed "
                "deg-per-frame aims the arm at the wrong part of the room")
        return float(yaw), float(wrist)

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
                loaded = roommap_mod.RoomMap.from_dict(json.load(f))
            if (self.roommap.view is not None and loaded.view is None
                    and loaded.places):
                # A map filed with the flat constants cannot be mixed with
                # directions worked out on the sphere: the same object would
                # land in two places, one per geometry. Keep the old one on
                # disk and start this geometry clean.
                os.replace(path, path + ".flat.json")
                self.note(f"room map was flat geometry; archived to "
                          f"{path}.flat.json, starting a world-geometry map")
                return False
            self.roommap = loaded
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
 .ask{display:flex;gap:6px;margin:0 0 6px}
 .ask input{flex:1;min-width:0;font:inherit;padding:7px 10px;border-radius:7px;
            border:1px solid #3a3a42;background:#18181b;color:#e7e7ea}
 #said{min-height:20px;font-size:14px;color:#a7f3d0;margin:0 0 4px}
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
    <div class="sec" style="border-top:0;margin-top:0;padding-top:0">ask avi</div>
    <form class="ask" onsubmit="ask(); return false;">
      <input id="askin" autocomplete="off"
             placeholder="where's my pen? / set a timer for 10 minutes">
      <button class="pri" type="submit">ask</button>
    </form>
    <div id="said"></div>
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

async function ask(){
  const box = document.getElementById("askin");
  const out = document.getElementById("said");
  const text = box.value.trim();
  if (!text) return;
  out.textContent = "...";
  try {
    const r = await api("/ask", {text: text});
    if (r.error && !r.say) out.textContent = "(" + r.error + ")";
    else out.textContent = r.say ||
      (r.gesture ? "(" + r.gesture + ")" : "(done)");
    box.value = "";
  } catch (e) {
    out.textContent = "(no answer from the robot)";
  }
}

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
    brain = st.get("brain")

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
                "presence": arm.presence.summary(),
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
            elif self.path == "/presence":
                pr = arm.presence
                self._json({"summary": pr.summary(),
                            "break": arm.coach.status(),
                            "recent": pr.recent(int(body.get("recent", 15))),
                            "routine": pr.routine(),
                            "sessions": [[round(a), None if b is None
                                          else round(b)]
                                         for a, b in pr.sessions()[-20:]],
                            "seat_view": arm.SEAT_VIEW,
                            "looking_at_seat": arm.view_ok_for_presence()})
            elif self.path == "/desk_scan":
                self._json(desk_scan(arm, latest,
                                     min_conf=float(body.get("min_conf", 0.30))))
            elif self.path == "/ask":
                if brain is None:
                    self._json({"error": "no brain in this process"}, 503)
                else:
                    self._json(brain.ask(body.get("text", "")))
            elif self.path == "/desk_layout":
                ts, objects = arm.desk.last()
                placed = [o for o in (objects or []) if "x" in o]
                lens_m = float(arm.config.raw.get("horizon", {}).get(
                    "lens_height_m", 0.0) or 0.0)
                self._json({"scanned_at": ts,
                            "units": ("lens heights above the desk, and "
                                      f"metres (lens at {lens_m} m)"
                                      if lens_m else
                                      "lens heights above the desk "
                                      "(unmeasured: relative only)"),
                            "viewpoint": "AVI's, from its base",
                            "objects": spatial_mod.layout(placed, lens_m),
                            "unplaced": [o["label"] for o in (objects or [])
                                         if "x" not in o]})
            elif self.path == "/desk_history":
                self._json({"changes": arm.desk.history(
                    label=body.get("label"), limit=int(body.get("limit", 50)))})
            elif self.path == "/find":
                self._json(find_object(arm, (body.get("name") or "").strip(),
                                       look=bool(body.get("look", True))))
            elif self.path == "/sweep_report":
                self._json(sweep_report(name=body.get("name", "sweep"),
                                        draws=int(body.get("draws", 999))))
            elif self.path == "/sweep_dome":
                self._json(sweep_dome(
                    arm, latest,
                    el_step=float(body.get("el_step", 4.0)),
                    el_start=float(body.get("el_start", 0.0)),
                    el_limit=(None if body.get("el_limit") is None
                              else float(body["el_limit"])),
                    azimuths=body.get("azimuths") or (0.0,),
                    dwell=float(body.get("dwell", 1.6)),
                    creep=float(body.get("creep", 2.5)),
                    settle_frames=int(body.get("settle_frames", 6)),
                    clip_frames=(None if body.get("clip_frames") is None
                                 else int(body["clip_frames"])),
                    out=body.get("out", "sweep"),
                    order=body.get("order", "down"),
                    seed=int(body.get("seed", 0))))
            elif self.path == "/build_dome":
                # magnitudes, frames and settle were accepted in the body and
                # then dropped on the floor, so several runs that looked like
                # they swept 2/4/6/8 actually swept step x 1,2,3. A parameter
                # that is read and ignored is worse than one that is absent.
                self._json(build_dome(
                    arm, latest,
                    step=float(body.get("step", 5.0)),
                    min_conf=float(body.get("min_conf", 0.40)),
                    prefer=body.get("prefer", poses.OBSERVE_POSE),
                    frames=int(body.get("frames", 5)),
                    settle=float(body.get("settle", 0.8)),
                    approach=float(body.get("approach", 12.0)),
                    yaw_scale=float(body.get("yaw_scale", 4.0)),
                    magnitudes=body.get("magnitudes"),
                    start_el=(None if body.get("start_el") is None
                              else float(body["start_el"]))))
            elif self.path == "/probe_horizon":
                self._json(probe_horizon(
                    arm, latest, delta=float(body.get("delta", 8.0)),
                    wrists=body.get("wrists"),
                    approach=float(body.get("approach", 24.0))))
            elif self.path == "/probe_backlash":
                self._json(probe_backlash(
                    arm, latest, yaw=float(body.get("yaw", 90.0)),
                    wrist=float(body.get("wrist", 150.0))))
            elif self.path == "/probe_depth":
                self._json(probe_depth(
                    arm, latest,
                    swing=float(body.get("swing", 40.0)),
                    steps=int(body.get("steps", 9)),
                    min_conf=float(body.get("min_conf", 0.35)),
                    frames=int(body.get("frames", 5))))
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
                    pose=body.get("pose", dome_mod.POLE),
                    el_step=float(body.get("el_step", 12.0)),
                    az_step=float(body.get("az_step", 16.0)),
                    el_limit=float(body.get("el_limit", 60.0)),
                    el_start=float(body.get("el_start", 12.0)),
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
                try:
                    settings_mod.save(settings_mod.path(), "latents", want)
                except OSError as exc:
                    arm.note(f"could not remember latents={want}: {exc!r}")
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


def _frame(latest, after=-1):
    """One decoded frame, and the sequence number it came from.

    `after` is what makes repeated calls return DIFFERENT images. Passing -1
    asks for whatever is current, which returns immediately and hands back the
    same frame every time if called in a loop.

    That was not hypothetical. A survey asking for three frames per stop
    logged `laptop:0.89, laptop:0.89, laptop:0.89` -- identical to two
    decimals, because it was one frame decoded three times. Worse, the
    median-of-nine added to build_dome to beat 12-21 px of box jitter was
    taking the median of nine copies of one frame, so it reduced nothing and
    the gains stayed exactly as noisy as before the fix.

    Callers that want N independent looks must thread the returned sequence
    back in. _look() does that.
    """
    import cv2
    import numpy as np
    for _ in range(40):
        jpeg, seq = latest.get_after(after, timeout=2.0)
        if jpeg:
            f = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if f is not None:
                return f, seq
    return None, after


def _look(latest, n):
    """Yield up to `n` genuinely distinct frames, newest sequence onwards."""
    seq = -1
    for _ in range(int(n)):
        frame, seq = _frame(latest, after=seq)
        if frame is None:
            return
        yield frame


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
    frame, _ = _frame(latest)
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


# A box within this many pixels of the frame edge is treated as cut off.
EDGE_PX = 6

# Wrist stops are all reached going DOWN; a column's first stop gets this
# much run-up from above. The wrist's own backlash is unmeasured, so this is
# consistency, not a measured number.
WRIST_RUN_UP_DEG = 6.0

# Detections this weak are still reported, never recorded. The point is to
# tell "the wall is empty" apart from "the desk was there at 0.22".
REPORT_FLOOR = 0.10


def _yaw_to(arm, target, settle):
    """Put base_yaw at `target`, last move upward (motion/backlash.py).
    Returns whether the backlash is known to be taken up -- False near the
    soft limit, where the run-up gets cut short."""
    lo, _ = arm.config.soft_limits("base_yaw")
    slack = getattr(arm, "yaw_slack", None)
    if slack is None:
        slack = arm.yaw_slack = backlash_mod.Slack()
    here = arm.snapshot().get("base_yaw")
    known = (slack.settled and here is not None and slack.last is not None
             and abs(float(here) - slack.last) < 1e-6)
    cmds = backlash_mod.plan(here, target, lo, settled=known)
    for i, cmd in enumerate(cmds):
        arm.set("base_yaw", cmd)
        slack.command(here, cmd)
        if i < len(cmds) - 1:
            time.sleep(max(settle * 0.6, backlash_mod.travel_s(here, cmd)))
        here = cmd
    return slack.settled


def _clipped(d, frame):
    """Is this box cut off by the frame edge? A clipped box's centre is not
    the object's centre, and it slides at the wrong rate as the object
    leaves the frame."""
    fh, fw = frame.shape[:2]
    return (d["cx"] - d["w"] / 2 <= EDGE_PX
            or d["cx"] + d["w"] / 2 >= fw - EDGE_PX
            or d["cy"] - d["h"] / 2 <= EDGE_PX
            or d["cy"] + d["h"] / 2 >= fh - EDGE_PX)


def run_survey(arm, latest, levels=None, min_conf=0.30, settle=1.8,
               frames=2, pose=None, el_step=12.0, az_step=16.0,
               el_limit=60.0, el_start=12.0):
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
    pose = pose or poses.OBSERVE_POSE
    if pose and taught and pose in taught:
        arm.set("shoulder", taught[pose]["shoulder"])
        arm.set("elbow", taught[pose]["elbow"])
        time.sleep(settle)
    shape = arm.snapshot()

    lo, hi = arm.config.soft_limits("base_yaw")
    wlo, whi = arm.config.soft_limits("wrist")

    # Stops come from the DOME, on the pole's own shape, which is Shaurya's
    # instruction and also the only geometry here that is exact. Holding
    # shoulder and elbow at TALL's values means the camera rotates in place:
    # base_yaw pans, the wrist tilts, nothing translates. The old uniform
    # sweep moved all four joints and so moved the lens as well as its aim,
    # which is why its results could not be reproduced across shapes.
    dome = None
    g = arm.config.dome
    if g.get("measured"):
        try:
            dome = dome_mod.Dome(
                taught,
                gains={k: float(g[k]) for k in
                       ("base_yaw", "shoulder", "elbow", "wrist")},
                limits={j: arm.config.soft_limits(j) for j in poses.JOINTS})
        except dome_mod.OffDome as exc:
            log(f"survey: no dome ({exc}); falling back to the flat sweep")

    if dome is not None:
        # Only the band the desk is actually in. The wrist spans 219 degrees
        # of elevation from the pole, and most of that is ceiling or carpet.
        grid = dome.grid(el_step=float(el_step), az_step=float(az_step),
                         limit=float(el_limit), start=float(el_start))
        stops = [(p["base_yaw"], p["wrist"]) for _, _, p in grid]
        # A stop closer to soft_min than the backlash can never get a full
        # run-up, so it is never settled and everything it sees is thrown
        # away. Visiting it anyway was ~18 of 99 moves per dense survey --
        # servo work on a servo that stalls under sustained work. Objects out
        # there are still seen at the left edge of the first settled column.
        floor = lo + backlash_mod.YAW_BACKLASH_DEG
        skipped = [st for st in stops if st[0] < floor]
        stops = [st for st in stops if st[0] >= floor]
        if skipped:
            log(f"survey: skipping {len(skipped)} stops below yaw "
                f"{floor:.0f}, which cannot be approached from below")
        # Column by column: the base steps up once per column instead of
        # sweeping and jumping back every row (motion/backlash.py).
        stops = backlash_mod.column_order(stops)
        shape_src = dome.pole_shape()
        arm.set("shoulder", shape_src["shoulder"])
        arm.set("elbow", shape_src["elbow"])
        time.sleep(settle)
        # RE-READ the shape now that it has changed. It used to be captured
        # above, before this branch moved shoulder and elbow to the pole's
        # values, and every sighting was then filed under the old shape. It
        # only worked because the /survey route happened to pass pose=TALL;
        # desk_scan called this without a pose, got ATTENTIVE's shape, and the
        # map rightly refused to mix the two.
        shape = arm.snapshot()
        log(f"survey: dome grid on {dome.pole}'s shape, {len(stops)} stops, "
            f"elevation {el_start}..{el_limit} in {el_step}, azimuth step "
            f"{az_step}")
    else:
        if not levels:
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
    # Every kept detection's DIRECTION, for desk_changes: the map merges and
    # decays, which is wrong for "is the pen there NOW", so a desk scan
    # snapshots what this survey actually saw. Labels canonicalised so a mug
    # read as "cup" on one scan cannot pass for something new.
    sightings, visited_stops = [], []
    # The previous stop's picture, to check the arm actually moved: base_yaw
    # once went silent mid-survey and eight stops filed one picture under
    # eight yaws. A turn that changes nothing is not trusted; two in a row
    # end the survey.
    # Compared against the SAME wrist level in the previous column: column
    # order means consecutive stops share a yaw, so the base's last move is
    # judged where the picture can show it.
    seen_at, stuck_run, stuck_total = {}, 0, 0
    last_wrist = None
    for yaw, wrist in stops:
        settled = _yaw_to(arm, yaw, settle)
        if last_wrist is not None and wrist > last_wrist:
            # A new column starts at the top: arrive going DOWN like every
            # other wrist stop in the survey.
            arm.set("wrist", min(whi, wrist + WRIST_RUN_UP_DEG))
            time.sleep(settle * 0.6)
        arm.set("wrist", wrist)
        last_wrist = wrist
        time.sleep(settle)
        frames_here = list(_look(latest, frames))
        moved_ok = True
        prev = seen_at.get(round(wrist, 1))
        # Only judged once the slack is taken up: a turn still eating the
        # backlash legitimately barely moves the picture.
        if (settled and prev is not None and frames_here
                and abs(yaw - prev[0]) >= horizon_mod.STUCK_MIN_TURN_DEG):
            try:
                reg = horizon_mod.register(prev[1], frames_here[-1])
            except horizon_mod.NotMeasurable:
                reg = None
            if horizon_mod.stuck(reg, yaw - prev[0]):
                moved_ok = False
        if frames_here:
            seen_at[round(wrist, 1)] = (yaw, frames_here[-1])
        if not moved_ok:
            stuck_run += 1
            stuck_total += 1
            log(f"survey: base_yaw did not move to {yaw:.1f} "
                f"(picture unchanged); stop not filed")
            if stuck_run >= 2:
                arm.set_pose(start)
                arm.save_roommap()
                return {"error": "base_yaw stopped responding: the picture "
                                 "did not change across two commanded turns",
                        "hint": "check the servo and its supply; restarting "
                                "the dashboard resets the Arduino",
                        "stopped_at": [yaw, wrist], "per_stop": per_stop,
                        "stuck_stops": stuck_total}
        else:
            stuck_run = 0
        settled = settled and moved_ok
        looked = {"yaw": yaw, "wrist": wrist, "frames": 0,
                  "yaw_settled": settled, "moved": moved_ok,
                  "kept": [], "below_floor": [], "people": 0,
                  # Raw boxes, so a direction model can be checked against
                  # the real detections after the fact (labels and numbers,
                  # never pixels).
                  "boxes": []}
        for frame in frames_here:
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
                looked["boxes"].append(
                    [roommap_mod.canonical_label(d["label"]),
                     round(float(d["conf"]), 3), int(d["cx"]), int(d["cy"]),
                     int(d["w"]), int(d["h"]), bool(_clipped(d, frame))])
                # A box cut off by the frame edge covers only the visible part
                # of the object, so its centre is pulled toward the middle of
                # the frame and the "direction" depends on which stop saw it.
                # The first desk scan split the laptop, the backpack and a pen
                # into pairs 13-16 degrees apart that way. Those boxes stay
                # out of the snapshot; an object is still caught from the
                # stops that see it whole.
                if not settled:
                    # Where it looked is uncertain by up to the backlash, so
                    # nothing seen here is filed anywhere.
                    continue
                if not _clipped(d, frame):
                    dy, dw = arm.roommap.direction_of(d["cx"], d["cy"],
                                                      yaw, wrist)
                    row = (roommap_mod.canonical_label(d["label"]),
                           round(dy, 2), round(dw, 2),
                           round(float(d["conf"]), 3))
                    # Where the box's BOTTOM edge meets the desk, in lens
                    # heights -- the object's foot, not its middle.
                    view = arm.roommap.view
                    if view is not None:
                        off = float(arm.config.raw.get("horizon", {}).get(
                            "offset_over_height", 0.0))
                        xy = view.on_plane(
                            d["cx"], d["cy"] + d["h"] / 2.0, yaw, wrist, off)
                        if xy is not None:
                            # And how tall: its top edge, standing over
                            # that foot.
                            z = view.height_over(
                                d["cx"], d["cy"] - d["h"] / 2.0, xy, yaw,
                                wrist, off)
                            row = row + ((round(xy[0], 3), round(xy[1], 3),
                                          None if z is None
                                          else round(z, 3)),)
                    sightings.append(row)
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
        if settled:
            visited_stops.append((float(yaw), float(wrist)))
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
            "sightings": sightings, "visited_stops": visited_stops,
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


def desk_scan(arm, latest, min_conf=0.30, el_step=10.0, az_step=16.0,
              el_start=12.0, el_limit=52.0):
    """Survey the desk band, snapshot it, and record what changed.

    Logs whether he was at the desk at the time, because that column is what
    turns "the backpack disappeared" into "the backpack leaves when he does".
    Ends at the seat view so presence keeps counting afterwards.
    """
    here = arm.presence.here
    res = run_survey(arm, latest, min_conf=min_conf, settle=1.0, frames=3,
                     pose=dome_mod.POLE, el_step=el_step, az_step=az_step,
                     el_start=el_start, el_limit=el_limit)
    seat = (load_taught() or {}).get(arm.SEAT_VIEW)
    if seat:
        arm.set_pose(seat)
    if res.get("error"):
        return res
    objects = desk_mod.snapshot(res.get("sightings") or [])
    half = (arm.roommap.yaw_deg_per_frame / 2.0,
            arm.roommap.wrist_deg_per_frame / 2.0)
    changes = arm.desk.record(objects, res.get("visited_stops") or [], half,
                              here=here)
    log(f"desk_scan: {len(objects)} objects, here={here}, changes "
        + ("none yet (first scan)" if changes is None else
           ", ".join(f"{k} {len(changes[k])}" for k in
                     ("appeared", "disappeared", "moved"))))
    return {"objects": objects, "changes": changes, "here": here,
            "stops": res.get("stops"), "sightings": len(res.get("sightings")
                                                         or [])}


def find_object(arm, name, look=True):
    """Where is it, from the room map -- and turn to look.

    The voice brain (avi_bot.main.Avi) is not wired to this process's arm in
    production; nothing constructs it with one. So this is the path on which
    "where's my pen" actually reaches the map built by the surveys. Map-only:
    this process has no sightings database, so a thing the map does not know
    is reported as unknown rather than guessed.
    """
    yaw_dpf, wrist_dpf = arm.frame_constants(arm.config)
    f = find_mod.Finder(None, yaw_dpf, wrist_deg_per_frame=wrist_dpf,
                        roommap=arm.roommap)
    text, _, _ = f.find(name)
    aim = f.aim(name)
    where = None
    try:
        _, objects = arm.desk.last()
        placed = [o for o in (objects or []) if "x" in o]
        where = spatial_mod.describe(roommap_mod.canonical_label(name),
                                     placed)
    except Exception as exc:                     # a layout is a bonus
        log(f"find: no layout ({exc!r})")
    if where:
        text = f"{text} {where}"
    moved = False
    if look and aim and aim.get("confident"):
        for joint in ("shoulder", "elbow", "base_yaw", "wrist"):
            if aim.get(joint) is None:
                continue
            if joint == "base_yaw":
                _yaw_to(arm, aim[joint], 1.0)
            else:
                arm.set(joint, aim[joint])
        moved = True
    log(f"find {name!r}: {text} moved={moved}")
    return {"say": text, "aim": aim, "moved": moved,
            "known": [p.label for p, _ in arm.roommap.known()]}


def _pixel_distances(thumbs):
    """Cosine distances between mean-centred thumbnails. Images discarded.

    Mean-centring per thumbnail removes overall brightness, which the probe
    already controls for separately, so this measures LAYOUT likeness.
    """
    import numpy as np
    T = np.vstack(thumbs).astype(np.float64)
    T = T - T.mean(axis=1, keepdims=True)
    n = np.linalg.norm(T, axis=1, keepdims=True)
    n[n == 0] = 1.0
    U = T / n
    return (1.0 - U @ U.T).astype(np.float32)


def sweep_report(name="sweep", draws=999):
    """Run view_probe on a saved sweep. Vectors only -- no frame is read.

    Done here rather than on the Mac so the latents never leave the MSI; they
    are not images, but there is no reason to move them either.
    """
    import os
    import numpy as np
    from avi_bot.perception import view_probe
    path = os.path.join(os.environ.get("USERPROFILE", "."), "avi-remote",
                        f"{name}-dev.npz")
    if not os.path.exists(path):
        return {"error": f"no sweep at {path}; run POST /sweep_dome first"}
    z = np.load(path)
    X, el = z["latents"], z["elevation"]
    t = z["t"] if "t" in z.files else None
    light = z["light"] if "light" in z.files else None
    out = {"file": path, "levels": int(len(el)), "dim": int(X.shape[1]),
           "has_time": t is not None, "has_light": light is not None}
    if t is None:
        # Sweeps saved before timestamps existed were all top-to-bottom, so
        # visit order IS elevation order. Reconstructing that is honest; it
        # also guarantees the confound check fires, which is the point.
        t = np.argsort(np.argsort(-el)).astype(float)
        out["time_reconstructed_from_order"] = True
    try:
        out["mantel"] = view_probe.mantel(X, el, t=t, light=light,
                                          draws=int(draws))
        out["nn"] = view_probe.nn_adjacent(X, el)
        if "pixel_dist" in z.files:
            out["baseline"] = view_probe.compare(
                X, z["pixel_dist"], el, t=t, light=light, draws=int(draws))
        else:
            out["baseline"] = {"verdict": "no pixel baseline in this sweep; "
                                          "re-run sweep_dome to get one"}
    except view_probe.NotEnoughData as exc:
        out["error"] = str(exc)
    return out


def sweep_dome(arm, latest, el_step=4.0, el_start=0.0, el_limit=None,
               azimuths=(0.0,), dwell=1.6, creep=2.5, settle_frames=6,
               clip_frames=None, out="sweep", order="down", seed=0):
    """Walk every level of the hemisphere, encode a V-JEPA latent at each, and
    PROVE the video actually swept.

    Three things it does that matter.

    SLOW, to keep motion blur out of the clips. The arm creeps to each level in
    `creep` degree increments rather than one commanded jump, then waits
    `dwell`, then discards `settle_frames` frames before any are kept. A clip
    containing the tail of a movement is a smeared clip, and a world model
    trained on smear learns smear.

    LABELLED. Each latent is stored with the (azimuth, elevation) it was taken
    at, so the latents have ground truth attached. That turns a pile of vectors
    into a dataset: depth_probe can ask whether the representation can be
    decoded back to where the camera was pointing, which is the actual test of
    whether it encodes space or only texture.

    VERIFIED, which is the part that needs doing because the obvious failure is
    silent. _frame used to hand back the same image on every call -- a survey
    asking three frames per stop logged laptop:0.89 three times, identical to
    two decimals -- so a sweep could look complete while every level held the
    same picture. Two numbers settle it per level:

        between  mean |pixel| difference against the PREVIOUS level
        within   same, between the first and last frame of this level's clip

    A real sweep of stationary views has `between` much larger than `within`:
    the view changed between levels and did not change during one. Both near
    zero means the camera never moved. `within` as large as `between` means the
    clips are catching the arm still moving, so slow it down further.
    """
    import cv2
    import numpy as np
    import latent_log

    taught = load_taught() or {}
    if dome_mod.POLE not in taught:
        return {"error": f"{dome_mod.POLE} is not taught; the dome has no pole"}
    g = arm.config.dome
    if not g.get("measured"):
        return {"error": "dome gains are unmeasured; run POST /build_dome "
                         "first or the elevation labels are guesses"}
    dome = dome_mod.Dome(
        taught,
        gains={k: float(g[k]) for k in
               ("base_yaw", "shoulder", "elbow", "wrist")},
        limits={j: arm.config.soft_limits(j) for j in poses.JOINTS})

    n_frames = int(clip_frames or latent_log.CLIP_FRAMES)
    start = arm.snapshot()
    shape = dome.pole_shape()
    arm.set("shoulder", shape["shoulder"])
    arm.set("elbow", shape["elbow"])
    time.sleep(dwell)

    rungs = dome.ladder(step=float(el_step), start=float(el_start),
                        limit=el_limit)
    levels = [el for el, _ in rungs]
    # ORDER matters for anything learned from this. A single descending pass
    # confounds elevation with time: level k is always captured just after
    # k-1, so slow drift -- light, auto-exposure, sensor warm-up -- makes
    # neighbouring elevations look alike for reasons unrelated to the view,
    # and that is indistinguishable from "the latent encodes elevation".
    # A shuffled order decorrelates elevation from capture time by design.
    if order == "up":
        levels = list(reversed(levels))
    elif order == "shuffle":
        import random
        levels = list(levels)
        random.Random(int(seed)).shuffle(levels)
    if not levels:
        arm.set_pose(start)
        return {"error": "no reachable levels between "
                         f"{el_start} and {el_limit}"}

    enc = arm.latent_encoder
    if enc is None:
        arm.note("sweep_dome: loading V-JEPA 2 ...")
        try:
            enc = arm.latent_encoder = latent_log.Encoder()
        except Exception as exc:
            arm.set_pose(start)
            return {"error": f"could not load the encoder: {exc!r}"}

    def creep_to(joint, target):
        """Arrive in small increments, so nothing is moving when we look."""
        here = arm.snapshot()[joint]
        span = target - here
        n = max(1, int(abs(span) / max(0.5, float(creep))))
        for i in range(1, n + 1):
            arm.set(joint, here + span * i / n)
            time.sleep(0.12)
        time.sleep(float(dwell))

    rows, latents, prev_first = [], [], None
    # Tiny grayscale thumbnails for the PIXEL BASELINE. Held in memory only;
    # what is saved is the pairwise distance matrix, not the images, because
    # the camera faces his room. 32x18 is enough to say "these two views look
    # alike" and far too small to show anything.
    thumbs = []
    t_start = time.time()
    for az in [float(a) for a in azimuths]:
        try:
            creep_to("base_yaw", dome.pose_at(az, levels[0])["base_yaw"])
        except dome_mod.OffDome as exc:
            rows.append({"azimuth": az, "skipped": str(exc)})
            continue
        for el in levels:
            try:
                want = dome.pose_at(az, el)
            except dome_mod.OffDome as exc:
                rows.append({"azimuth": az, "elevation": el,
                             "skipped": str(exc)})
                continue
            creep_to("wrist", want["wrist"])

            # Throw away frames that may still hold the tail of the move.
            seq = -1
            for _ in range(int(settle_frames)):
                _, seq = _frame(latest, after=seq)

            frames, seqs, first, last = [], [], None, None
            for _ in range(n_frames):
                bgr, seq = _frame(latest, after=seq)
                if bgr is None:
                    break
                seqs.append(seq)
                g0 = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                if first is None:
                    first = g0
                last = g0
                frames.append(cv2.resize(
                    cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB),
                    (latent_log.CLIP_SIZE, latent_log.CLIP_SIZE),
                    interpolation=cv2.INTER_AREA))
            if len(frames) < n_frames or first is None:
                rows.append({"azimuth": az, "elevation": el,
                             "skipped": f"only {len(frames)} frames arrived"})
                continue

            within = float(np.mean(np.abs(
                first.astype(np.int16) - last.astype(np.int16))))
            between = (None if prev_first is None else float(np.mean(np.abs(
                first.astype(np.int16) - prev_first.astype(np.int16)))))
            prev_first = first

            # latent_log.Encoder is CALLED, and returns (vector, ms). The first
            # draft of this called a .encode() that does not exist, which would
            # have failed on the first level of the first sweep -- and only on
            # the MSI, because this file cannot be imported on the Mac.
            try:
                vec, ms = enc(frames)
            except Exception as exc:
                arm.set_pose(start)
                return {"error": f"encode failed at az {az} el {el}: {exc!r}",
                        "rows": rows}
            latents.append(np.asarray(vec, dtype=np.float32).ravel())
            thumbs.append(cv2.resize(first, (32, 18),
                                     interpolation=cv2.INTER_AREA)
                          .astype(np.float32).ravel())
            rows.append({
                "t": round(time.time() - t_start, 2),
                "visit": len(latents) - 1,
                "azimuth": round(az, 1), "elevation": round(el, 1),
                "wrist": round(want["wrist"], 1),
                "base_yaw": round(want["base_yaw"], 1),
                "frames": len(frames),
                "distinct_seqs": len(set(seqs)),
                "within": round(within, 2),
                "between": None if between is None else round(between, 2),
                "light": round(float(first.mean()), 1),
                "sd": round(float(first.std()), 1),
                "encode_ms": round(ms, 1)})
    arm.set_pose(start)

    got = [r for r in rows if "within" in r]
    out_path = None
    if latents:
        import os
        d = os.path.join(os.environ.get("USERPROFILE", "."), "avi-remote")
        os.makedirs(d, exist_ok=True)
        out_path = os.path.join(d, f"{out}-dev.npz")
        np.savez_compressed(
            out_path,
            latents=np.vstack(latents),
            azimuth=np.array([r["azimuth"] for r in got], dtype=np.float32),
            elevation=np.array([r["elevation"] for r in got], dtype=np.float32),
            t=np.array([r["t"] for r in got], dtype=np.float32),
            pixel_dist=_pixel_distances(thumbs),
            light=np.array([r["light"] for r in got], dtype=np.float32),
            sd=np.array([r["sd"] for r in got], dtype=np.float32))

    res = {"levels": len(levels), "azimuths": list(azimuths), "order": order,
           "el_step": el_step, "measured": len(got), "saved": out_path,
           "dim": int(latents[0].size) if latents else 0,
           "clip_frames": n_frames, "rows": rows}

    betweens = [r["between"] for r in got if r["between"] is not None]
    withins = [r["within"] for r in got]
    stale = [r for r in got if r["distinct_seqs"] < r["frames"]]
    if betweens:
        b = sum(betweens) / len(betweens)
        w = sum(withins) / len(withins)
        res["mean_between"] = round(b, 2)
        res["mean_within"] = round(w, 2)
        res["ratio"] = round(b / w, 2) if w > 0.01 else None
        res["repeated_frames"] = len(stale)
        if b < 1.0:
            res["verdict"] = ("THE VIDEO DID NOT SWEEP. Consecutive levels "
                              "are the same picture -- the arm did not move, "
                              "or the stream is frozen.")
        elif w > 0.6 * b:
            res["verdict"] = ("SWEPT, BUT BLURRED. Frames change as much "
                              "DURING a level as between levels, so the clips "
                              "are catching the arm still moving. Raise dwell "
                              "or settle_frames.")
        elif stale:
            res["verdict"] = (f"SWEPT, but {len(stale)} levels reused a frame; "
                              "the stream is slower than the clip length.")
        else:
            res["verdict"] = ("THE VIDEO SWEPT. Each level is a distinct, "
                              f"stationary view: between {b:.1f} vs within "
                              f"{w:.1f}.")
    log(f"sweep_dome: {len(got)}/{len(levels) * len(azimuths)} levels, "
        f"{res.get('verdict', 'no verdict')[:60]}")
    return res


def build_dome(arm, latest, step=5.0, settle=0.8, min_conf=0.40,
               prefer=None, frames=5, magnitudes=None, yaw_scale=4.0,
               approach=12.0, start_el=None):
    """Learn the dome's joint-to-direction map from the arm's own motion.

    What Shaurya asked for: establish a dome with TALL at the top, then move
    the servos and derive the rest of the frame from what that motion does to
    the view. Each joint is moved ALONE by a known amount while a fixed
    landmark is tracked; how far the landmark slid IS that joint's effect on
    the look direction. It is the measurement that produced
    wrist_deg_per_frame, generalised to all four joints and solved together.

    Moving one joint at a time is not a convenience. If shoulder and elbow
    always move together their individual gains are not identifiable, and a
    fit would return an arbitrary split of one real number -- dome._fit_pitch
    refuses rather than inventing the split, and this is how the data is kept
    separable in the first place.

    Steps are small and the arm is returned to where it started. shoulder and
    elbow have never been exercised by a probe before, and they swing the
    whole arm rather than just the camera.
    """
    if arm.detector is None:
        from detector import DeskDetector
        arm.detector = DeskDetector()
    prefer = prefer or poses.OBSERVE_POSE
    taught = load_taught() or {}
    if dome_mod.POLE not in taught:
        return {"error": f"{dome_mod.POLE} is not taught; the dome has no "
                         f"pole. Teach it on the calibrate page first.",
                "taught": sorted(taught)}

    start = arm.snapshot()
    if start_el is not None:
        # Start ON THE DOME: TALL's shoulder and elbow, wrist at the given
        # elevation below the pole. Added because by early afternoon nothing
        # cleared 0.35 at HOME any more, while every survey from TALL's shape
        # saw the laptop at 0.89-0.93 around wrist 166 -- the measurement
        # should start where the robot can actually see something.
        g = arm.config.dome
        d0 = dome_mod.Dome(
            taught, gains={k: float(g.get(k, dome_mod.DEFAULT_GAINS[k]))
                           for k in ("base_yaw", "shoulder", "elbow", "wrist")},
            limits={j: arm.config.soft_limits(j) for j in poses.JOINTS})
        arm.set_pose(d0.pose_at(0.0, float(start_el)))
        time.sleep(settle * 2)
    elif prefer in taught:
        arm.set_pose(taught[prefer])
        time.sleep(settle)

    def best_landmark():
        """The strongest non-person box, and where it is."""
        frame, _ = _frame(latest)
        if frame is None:
            return None
        got = [d for d in arm.detector.everything(frame, min_conf=min_conf)
               if d["label"] != "person"]
        if not got:
            return None
        return max(got, key=lambda d: d["conf"])

    def go(joint, target, lo, hi):
        """Drive a joint to `target`, always arriving from BELOW it.

        base_yaw has severe asymmetric backlash. Measured on this arm with a
        shared home reference, positive travel was clean and monotonic --
        11.5, 13.3, 17.8, 19.1 px per degree at 2, 4, 8 and 16 degrees -- while
        negative travel barely moved at all: 2 and 4 degree commands produced
        3 px, which is noise, and 8 degrees produced motion the wrong way.

        Approaching every target from one side is what the diagnosis itself
        recommends, and it removes a confound this probe had introduced. An
        earlier version took its reference once per joint to save time, which
        meant negative samples arrived from the opposite side and measured
        backlash rather than gain.
        """
        pre = target - approach
        if pre < lo:
            pre = min(hi, target + approach)      # no room below; come down
        arm.set(joint, pre)
        time.sleep(settle * 0.6)
        arm.set(joint, target)
        time.sleep(settle)

    def find(label):
        """Where `label` is now: the MEDIAN over several frames.

        Measured on the real arm, a laptop's box centre scatters by 12 to 21
        px between frames -- the box edges wobble as the detector's confidence
        moves, and a large object's centre moves with them. At a 2 degree step
        base_yaw only shifts the view ~15 px, so the noise was the same size
        as the signal and its gain came back as 0.21, 0.24, 0.51 and 0.68 on
        four runs of the same arm.

        Median rather than mean because the failure is an occasional box
        snapping to a different extent, which is an outlier rather than
        Gaussian noise, and one of those drags a mean a long way.
        """
        xs, ys = [], []
        for frame in _look(latest, frames):
            for d in arm.detector.everything(frame, min_conf=min_conf):
                if d["label"] == label:
                    xs.append(d["cx"])
                    ys.append(d["cy"])
                    break
        if len(xs) < 3:
            return None
        mid = lambda v: sorted(v)[len(v) // 2]
        return mid(xs), mid(ys)

    anchor_det = best_landmark()
    if anchor_det is None:
        arm.set_pose(start)
        return {"error": "no landmark to track; nothing detected above "
                         f"{min_conf}. Put something recognisable in view.",
                "hint": "a laptop, a bottle or a mug works"}
    label = anchor_det["label"]

    # SEVERAL magnitudes per joint, not one. Two samples per joint is a fit
    # through two points: it cannot fail and it cannot be checked, which is
    # how base_yaw came back as 0.21, 0.24, 0.51 and 0.68 from the same arm
    # on four runs. More magnitudes give the residual something to say.
    mags = [float(m) for m in (magnitudes or (step, step * 2, step * 3))]
    # base_yaw gets BIGGER steps than the pitch joints, and that asymmetry is
    # measured rather than aesthetic. Per degree it moves the view three to
    # five times less than shoulder or elbow do, so the same step gives it a
    # far worse signal-to-noise ratio -- while it also has the most headroom
    # (20..160) to take a larger one without losing the landmark.
    per_joint = {"base_yaw": [m * yaw_scale for m in mags]}
    samples, lost = [], []
    for joint in ("base_yaw", "shoulder", "elbow", "wrist"):
        lo, hi = arm.config.soft_limits(joint)
        home = arm.snapshot()
        at_home = None
        for signed in [s for m in per_joint.get(joint, mags)
                       for s in (+m, -m)]:
            target = home[joint] + signed
            if not (lo <= target <= hi):
                lost.append(f"{joint}{signed:+.0f}: outside {lo:.0f}..{hi:.0f}")
                continue
            # The reference reading is taken ONCE per joint, at home, rather
            # than before every sample. The arm used to be driven out and all
            # the way back between samples, which paid two settles and two
            # travels for one measurement.
            if at_home is None:
                go(joint, home[joint], lo, hi)
                at_home = find(label)
                if at_home is None:
                    lost.append(f"{joint}: lost {label} at the home reading")
                    break
            before_j = dict(home)
            at_before = at_home
            go(joint, target, lo, hi)
            after_j = arm.snapshot()
            at_after = find(label)
            if at_after is None:
                lost.append(f"{joint}{signed:+.0f}: {label} left the frame")
                continue
            samples.append((before_j, after_j,
                            at_after[0] - at_before[0],
                            at_after[1] - at_before[1]))
        go(joint, home[joint], lo, hi)
    arm.set_pose(start)

    limits = {j: arm.config.soft_limits(j)
              for j in ("base_yaw", "shoulder", "elbow", "wrist")}
    d = dome_mod.Dome(taught, limits=limits)
    before_gains = dict(d.gains)
    learned = d.learn(samples)

    out = {"landmark": label, "step": step, "magnitudes": mags,
           "magnitudes_base_yaw": per_joint["base_yaw"], "frames": frames,
           "approach_deg": approach,
           "samples": len(samples), "fit": d.fit,
           "residuals": d.residuals,
           "diagnosis": {j: d.diagnose(j)
                         for j in ("base_yaw", "shoulder", "elbow", "wrist")},
           "lost": lost, "gains_before": before_gains,
           "learned": {k: round(v, 4) for k, v in learned.items()},
           "still_assumed": d.unknown(),
           "azimuth_limits": [round(v, 1) for v in d.azimuth_limits()]}
    if not learned:
        out["why_nothing"] = (
            "no gain was identifiable. Either too few usable samples, or the "
            "joints did not move independently enough to separate them.")
        log(f"build_dome: {len(samples)} samples, nothing identifiable")
        return out

    # Where the taught poses sit on the dome, in HIS terms, as the check that
    # the frame came out sane: TALL at 0, REST far below, the desk poses
    # bunched together.
    places = {}
    for name, pose in taught.items():
        try:
            az, el = d.direction_of(pose)
            places[name] = {"azimuth": round(az, 1), "elevation": round(el, 1)}
        except dome_mod.OffDome as exc:
            places[name] = {"off_dome": str(exc)}
    out["poses_on_dome"] = places
    log(f"build_dome: learned {out['learned']} from {len(samples)} samples "
        f"on {label}")
    return out


def probe_depth(arm, latest, swing=40.0, steps=9, settle=1.6,
                min_conf=0.35, frames=5,
                approach=backlash_mod.YAW_BACKLASH_DEG):
    """Pan once, track everything, and report range per object.

    Also settles the open question in config/joints.toml [kinematics]. The
    apparent shift rate is s = g * (1 + (r/rho) cos eta): rotation, which is
    the same for everything, plus parallax, which is not. So:

      * if base_yaw really UNDER-TRAVELS its command, every object in the
        scene shifts at nearly the same rate and the spread is small;
      * if the 0.62 is parallax, rates differ by tens of per cent between a
        desk object and the far wall.

    One pan decides it, and the answer is `rate_spread` below. Nothing here
    needs the kinematics to be measured: the ORDERING is calibration-free,
    which is why it is reported even when ranges cannot be.
    """
    import numpy as np
    if arm.detector is None:
        from detector import DeskDetector
        arm.detector = DeskDetector()
    start = arm.snapshot()
    lo, hi = arm.config.soft_limits("base_yaw")
    here = start["base_yaw"]
    half = swing / 2.0
    a, b = max(lo, here - half), min(hi, here + half)
    if b - a < depth_mod.MIN_SWING_DEG:
        return {"error": f"only {b - a:.1f} deg of yaw available here; "
                         f"need {depth_mod.MIN_SWING_DEG}",
                "hint": "move base_yaw toward the middle of its range first"}

    fov = arm.config.camera.get("fov_deg") or 52.0
    tracks, seen_at = {}, {}
    yaws = [a + (b - a) * i / (steps - 1) for i in range(steps)]
    # Arrive at the first stop from BELOW, like every later one. base_yaw's
    # backlash is asymmetric (build_dome measured it); a first sample that
    # came from the other side is offset by the backlash, not by parallax.
    arm.set("base_yaw", max(lo, a - approach))
    time.sleep(settle)
    for yaw in yaws:
        arm.set("base_yaw", yaw)
        time.sleep(settle)
        per = {}
        for frame in _look(latest, frames):
            best = {}
            for d in arm.detector.everything(frame, min_conf=min_conf):
                if d["label"] == "person":
                    continue        # people move; a moving point has no range
                if _clipped(d, frame):
                    continue        # its centre slides at the wrong rate
                # One box per label per frame, the strongest. Two boxes of
                # the same class cannot be told apart without a tracker, and
                # guessing the correspondence is how a wrong depth gets made.
                if d["conf"] > best.get(d["label"], {"conf": -1})["conf"]:
                    best[d["label"]] = d
            for k, d in best.items():
                per.setdefault(k, []).append(d["cx"])
        for k, xs in per.items():
            if 2 * len(xs) < frames:
                continue            # in under half the frames: a flicker
            # The MEDIAN over frames: a box centre scatters 12-21 px between
            # frames, which is the same size as the parallax being measured.
            seen_at.setdefault(k, []).append(yaw)
            tracks.setdefault(k, depth_mod.Track(k, fov)).add(
                yaw, float(np.median(xs)))
    arm.set_pose(start)

    out = {"swing": round(b - a, 1), "stops": steps, "fov_deg": fov,
           "labels": {k: len(v.xs) for k, v in tracks.items()}}
    if not tracks:
        out["error"] = "nothing detected across the pan"
        return out
    try:
        order, info = depth_mod.ordering(list(tracks.values()))
    except depth_mod.NotMeasurable as exc:
        out["error"] = str(exc)
        return out
    out["nearest_first"] = [[k, round(v, 4)] for k, v in order]
    out["rejected"] = info["rejected"]

    rates = [v for _, v in order]
    if len(rates) >= 2:
        spread = (max(rates) - min(rates)) / max(rates)
        out["rate_spread"] = round(spread, 4)
        out["reading"] = (
            "rates differ across the scene, so the 0.62 in [camera] is "
            "largely PARALLAX and yaw_deg_per_frame is distance-dependent"
            if spread > 0.12 else
            "rates agree across the scene, so the 0.62 is the joint "
            "UNDER-TRAVELLING its command, as config/joints.toml assumes")

    kin = arm.config.kinematics
    out["kinematics_measured"] = bool(kin.get("measured"))
    if kin.get("measured") and kin.get("camera_radius_m"):
        got, rinfo = depth_mod.ranges(list(tracks.values()),
                                      r=float(kin["camera_radius_m"]))
        out["ranges_m"] = {
            k: (None if v["range"] is None else round(v["range"], 3))
            for k, v in got.items()}
        out["range_info"] = {k: v for k, v in rinfo.items() if k != "rates"}
    else:
        out["why_no_ranges"] = kin.get("why", "camera_radius_m is 0")
    log(f"probe_depth: {len(tracks)} tracks, spread "
        f"{out.get('rate_spread')}, {out.get('reading', '')[:60]}")
    return out


def probe_horizon(arm, latest, wrists=None, delta=8.0, approach=24.0,
                  settle=1.6, centre=90.0):
    """Which way is level, the base's true gain, and the lens's true FOV.

    At each wrist tilt on TALL's shape, turn the base by `delta` twice and
    register the pictures: a base turn rolls the image by g sin(e) and slides
    it by g cos(e). See perception/horizon.py. Every stop is approached from
    below (base_yaw backlash), and each tilt measures two halves so the
    disagreement between them is visible as the noise floor.
    """
    taught = load_taught() or {}
    if dome_mod.POLE not in taught:
        return {"error": f"{dome_mod.POLE} is not taught"}
    start = arm.snapshot()
    wlo, whi = arm.config.soft_limits("wrist")
    ylo, yhi = arm.config.soft_limits("base_yaw")
    if wrists is None:
        top = min(whi, taught[dome_mod.POLE]["wrist"])
        wrists = [top - 12.0 * i for i in range(14) if top - 12.0 * i >= wlo]
    a, b, c = centre - delta, centre, centre + delta
    if a - approach < ylo or c > yhi:
        return {"error": f"yaw stops {a}..{c} fall outside {ylo}..{yhi}"}
    arm.set("shoulder", taught[dome_mod.POLE]["shoulder"])
    arm.set("elbow", taught[dome_mod.POLE]["elbow"])
    levels = []
    try:
        for w in wrists:
            arm.set("base_yaw", a - approach)
            pre = w - approach if w - approach >= wlo else min(whi, w + approach)
            arm.set("wrist", pre)
            time.sleep(settle * 0.6)
            arm.set("wrist", w)
            time.sleep(settle * 0.6)
            shots = []
            for yaw in (a, b, c):
                arm.set("base_yaw", yaw)
                time.sleep(settle)
                got = list(_look(latest, 3))
                shots.append(got[-1] if got else None)
            row = {"wrist": round(w, 1), "halves": []}
            for x, y in ((0, 1), (1, 2)):
                if shots[x] is None or shots[y] is None:
                    row["halves"].append({"error": "no picture"})
                    continue
                try:
                    r = horizon_mod.register(shots[x], shots[y])
                    row["halves"].append(
                        {"roll": round(r["roll_deg"] / delta, 4),
                         "slide": round(r["dx_px"] / delta, 2),
                         "rise": round(r["dy_px"] / delta, 2),
                         "scale": round(r["scale"], 4),
                         "inliers": r["inliers"]})
                except horizon_mod.NotMeasurable as exc:
                    row["halves"].append({"error": str(exc)})
            ok = [h for h in row["halves"] if "roll" in h]
            if len(ok) == 2:
                row["roll"] = round((ok[0]["roll"] + ok[1]["roll"]) / 2, 4)
                row["slide"] = round((ok[0]["slide"] + ok[1]["slide"]) / 2, 2)
                row["disagree"] = round(abs(ok[0]["roll"] - ok[1]["roll"]), 4)
            levels.append(row)
            log(f"probe_horizon: wrist {w:.0f} roll {row.get('roll')} "
                f"slide {row.get('slide')}")
    finally:
        arm.set_pose(start)

    out = {"delta": delta, "centre_yaw": centre, "levels": levels}
    good = [r for r in levels if "roll" in r
            and r["disagree"] <= horizon_mod.MAX_HALF_DISAGREE]
    out["used"] = [r["wrist"] for r in good]
    try:
        ws = [r["wrist"] for r in good]
        rolls = [r["roll"] for r in good]
        free = horizon_mod.fit_roll(ws, rolls, k_grid=[0.70 + 0.01 * i
                                                       for i in range(61)])
        fixed = horizon_mod.fit_roll(
            ws, rolls, k=float(arm.config.dome.get("wrist", 1.0)))
        out["fit"], out["fit_dome_k"] = free, fixed
        P, prms = horizon_mod.px_per_deg(ws, [r["slide"] for r in good], free)
        width = shots[0].shape[1] if shots and shots[0] is not None else 1280
        out["px_per_deg"] = round(P, 2)
        out["fov_deg"] = round(width / P, 1)
        out["slide_rms_px"] = round(prms, 2)
        out["reading"] = (
            f"base_yaw turns {free['g']:.2f}x its command; the view is level "
            f"at wrist {free['w_level']:.0f} on TALL's shape; lens FOV "
            f"{width / P:.1f} deg (config assumes 52)")
    except horizon_mod.NotMeasurable as exc:
        out["error"] = str(exc)
    return out


def probe_backlash(arm, latest, yaw=90.0, wrist=150.0, run=40.0, settle=2.0,
                   repeats=2):
    """base_yaw's backlash, in degrees, from two pictures.

    Arrive at `yaw` from below, then from above, and register the pictures.
    Their slide, divided by how far one degree of turn slides the picture at
    this tilt (g cos(e) px/deg, from [horizon]), is the backlash. Arriving
    from below twice gives the repeatability to judge it against.
    """
    h = arm.config.raw.get("horizon", {})
    if not h.get("measured"):
        return {"error": "[horizon] is not measured; POST /probe_horizon"}
    taught = load_taught() or {}
    start = arm.snapshot()
    lo, hi = arm.config.soft_limits("base_yaw")
    if yaw - run < lo or yaw + run > hi:
        return {"error": f"{yaw}+-{run} leaves {lo}..{hi}"}
    if dome_mod.POLE in taught:
        arm.set("shoulder", taught[dome_mod.POLE]["shoulder"])
        arm.set("elbow", taught[dome_mod.POLE]["elbow"])
    arm.set("wrist", wrist)

    def shot(frm):
        arm.set("base_yaw", frm)
        time.sleep(backlash_mod.travel_s(None, 0) / 2)
        arm.set("base_yaw", yaw)
        time.sleep(settle)
        got = list(_look(latest, 3))
        return got[-1] if got else None

    e = float(h.get("wrist_gain", 1.0)) * (wrist - float(h["level_wrist"]))
    per_deg = (float(h.get("yaw_gain", 1.0)) * math.cos(math.radians(e))
               * 1280.0 / float(h.get("fov_deg", 48.6)))
    out = {"yaw": yaw, "wrist": wrist, "elevation": round(e, 1),
           "px_per_deg_of_turn": round(per_deg, 2), "trials": []}
    try:
        for _ in range(int(repeats)):
            a, b, c = shot(yaw - run), shot(yaw + run), shot(yaw - run)
            trial = {}
            for name, (x, y) in (("below_vs_above", (a, b)),
                                 ("below_vs_below", (a, c))):
                try:
                    r = horizon_mod.register(x, y)
                    trial[name] = round(r["dx_px"] / per_deg, 2)
                    trial[name + "_inliers"] = r["inliers"]
                except (horizon_mod.NotMeasurable, TypeError) as exc:
                    trial[name] = None
                    trial[name + "_error"] = str(exc)
            out["trials"].append(trial)
    finally:
        arm.set_pose(start)
    got = [t["below_vs_above"] for t in out["trials"]
           if t.get("below_vs_above") is not None]
    rep = [t["below_vs_below"] for t in out["trials"]
           if t.get("below_vs_below") is not None]
    if got:
        out["backlash_deg"] = round(abs(sum(got) / len(got)), 2)
    if rep:
        out["repeatability_deg"] = round(max(abs(v) for v in rep), 2)
    log(f"probe_backlash: {out.get('backlash_deg')} deg "
        f"(repeat {out.get('repeatability_deg')})")
    return out


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
        for frame in _look(latest, frames):
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
    frame, _ = _frame(latest)
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
        frame, _ = _frame(latest)
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
    threading.Thread(target=arm.presence_loop, args=(latest, deadline),
                     daemon=True).start()
    threading.Thread(target=arm.pause_loop, args=(latest, deadline),
                     daemon=True).start()
    threading.Thread(target=arm.latent_loop, args=(latest, deadline),
                     daemon=True).start()
    brain = arm.make_brain_host(latest)
    threading.Thread(target=brain.loop, args=(deadline,),
                     daemon=True).start()

    srv = ThreadingHTTPServer((host, args.port), make_handler({
        "arm": arm, "latest": latest, "deadline": deadline, "brain": brain,
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
