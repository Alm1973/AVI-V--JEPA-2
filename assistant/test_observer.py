"""Self-checks that run without a GPU, camera or model download.

    python -m pytest test_observer.py -q     (or: python test_observer.py)
"""
import os
import random
import tempfile
from datetime import datetime

import numpy as np

import settings
from change import ChangeDetector
from memory import Memory
from observer import Observer
from vlm import LETTERS, choose_with

T0 = datetime(2026, 10, 2, 16, 0, 0).timestamp()


def blank(value=100):
    return np.full((720, 1280, 3), value, dtype=np.uint8)


def with_block(value=100, block=230, x=400):
    f = blank(value)
    f[200:520, x:x + 400] = block
    return f


# --- multiple choice ---------------------------------------------------
def test_choice_follows_content_not_position():
    def score(prompt, image, n):
        lines = prompt.splitlines()[1:1 + n]
        return [5.0 if "writing on paper" in ln else 0.0 for ln in lines]
    rng = random.Random(1)
    for _ in range(20):
        best, probs = choose_with(score, "q?", settings.ACTIVITIES, None, rng=rng)
        assert best == "writing on paper"
        assert probs[best] > 0.9


def test_position_bias_spreads_out_with_shuffling():
    def always_a(prompt, image, n):
        return [5.0] + [0.0] * (n - 1)
    best, probs = choose_with(always_a, "q?", settings.ACTIVITIES, None,
                              passes=8, rng=random.Random(2))
    # A model that always says "A" gets no single answer above the threshold.
    assert probs[best] < settings.ACTIVITY_MIN_P
    assert abs(sum(probs.values()) - 1.0) < 1e-9


def test_prompt_has_one_letter_per_option():
    seen = {}
    def capture(prompt, image, n):
        seen["prompt"] = prompt
        return [0.0] * n
    choose_with(capture, "Q", settings.ACTIVITIES, None, rng=random.Random(0))
    lines = seen["prompt"].splitlines()
    assert lines[0] == "Q" and lines[-1] == "Answer with the letter only."
    assert [ln[0] for ln in lines[1:-1]] == list(LETTERS[:len(settings.ACTIVITIES)])


# --- change detector ---------------------------------------------------
def test_static_scene_never_fires():
    cd = ChangeDetector()
    fired = [cd.update(blank(), T0 + i * 0.2) for i in range(100)]
    assert not any(fired)


def test_change_fires_once_after_settling():
    cd = ChangeDetector()
    cd.update(blank(), T0)
    fired = [cd.update(with_block(), T0 + 0.2 + i * 0.2) for i in range(30)]
    assert sum(fired) == 1
    # fires only after the scene has been still for SETTLE_S
    first = fired.index(True)
    assert (first) * 0.2 >= settings.SETTLE_S


def test_hand_wave_that_leaves_nothing_does_not_fire():
    cd = ChangeDetector()
    cd.update(blank(), T0)
    t = T0
    for _ in range(3):              # something passes through
        t += 0.2
        assert not cd.update(with_block(), t)
    fired = []
    for _ in range(30):             # back to the original scene
        t += 0.2
        fired.append(cd.update(blank(), t))
    assert not any(fired)


def test_constant_motion_forces_an_observation():
    cd = ChangeDetector()
    cd.update(blank(), T0)
    fired_at = None
    for i in range(1, 80):
        x = 100 + (i % 2) * 500    # the block keeps jumping around
        if cd.update(with_block(x=x), T0 + i * 0.2):
            fired_at = i * 0.2
            break
    assert fired_at is not None and fired_at >= settings.FORCE_AFTER_S
    assert cd.last_reason == "change-forced"


def test_suppression_ignores_self_motion():
    cd = ChangeDetector()
    cd.update(blank(), T0)
    cd.suppress(T0 + 5)
    fired = [cd.update(with_block(), T0 + 0.2 + i * 0.2) for i in range(20)]
    assert not any(fired)


# --- memory and sessions -----------------------------------------------
def add(mem, ts, activity, p=0.9):
    return mem.add_observation(ts=ts, trigger="heartbeat", activity=activity,
                               activity_raw=activity, activity_p=p,
                               probs={activity: p}, person_conf=0.5,
                               vlm_calls=1, latency_ms=300)


def test_sessions_merge_split_and_drop():
    mem = Memory(":memory:")
    day = datetime.fromtimestamp(T0).date().isoformat()
    t = T0
    for _ in range(10):                     # 10 min of writing
        add(mem, t, "writing on paper"); t += 60
    add(mem, t, settings.UNCERTAIN); t += 60  # one unsure look in the middle
    for _ in range(5):
        add(mem, t, "writing on paper"); t += 60
    add(mem, t, "phone"); t += 60            # 1 phone glance: dropped as too short
    for _ in range(8):
        add(mem, t, "laptop"); t += 60
    t += 1000                                # long gap
    for _ in range(4):
        add(mem, t, "laptop"); t += 60

    kept = mem.rebuild_sessions(day)
    names = [s["activity"] for s in kept]
    assert names == ["writing on paper", "laptop", "laptop"], names
    assert kept[0]["n_obs"] == 15            # the uncertain look didn't split it
    assert len(mem.sessions(day)) == 3
    assert mem.rebuild_sessions(day) == kept  # rebuilding is repeatable


def test_away_blocks_merge_and_unsure_runs_bridge():
    mem = Memory(":memory:")
    day = datetime.fromtimestamp(T0).date().isoformat()
    t = T0
    for _ in range(6):                           # 30 min away, 300 s heartbeat
        add(mem, t, settings.NOBODY); t += settings.AWAY_HEARTBEAT_S
    for _ in range(5):
        add(mem, t, "guitar"); t += 60
    for _ in range(3):                           # three unsure looks in a row
        add(mem, t, settings.UNCERTAIN); t += 60
    for _ in range(5):
        add(mem, t, "guitar"); t += 60
    kept = mem.rebuild_sessions(day)
    assert [s["activity"] for s in kept] == [settings.NOBODY, "guitar"]
    assert kept[0]["n_obs"] == 6 and kept[1]["n_obs"] == 10


def test_last_seen_ignores_rejected_sightings():
    mem = Memory(":memory:")
    s_yes = {"label": "calculator", "det_conf": 0.4, "vlm_margin": 2.1,
             "x": 0.05, "y": 0.5, "w": 0.1, "h": 0.1}
    s_no = dict(s_yes, vlm_margin=-1.0, x=0.8)
    mem.add_observation(T0, "change", "laptop", "laptop", 0.9, {"laptop": 0.9},
                        0.6, 3, 900, sightings=[s_yes])
    mem.add_observation(T0 + 60, "heartbeat", "laptop", "laptop", 0.9,
                        {"laptop": 0.9}, 0.6, 3, 900, sightings=[s_no])
    r = mem.last_seen("calculator")
    assert r["ts"] == T0 and r["zone"] == "left"


# --- observer loop -----------------------------------------------------
class FakeVLM:
    def __init__(self, label="laptop", p=0.9, present=("calculator",)):
        self.label, self.p, self.present, self.calls = label, p, present, 0

    def choose(self, question, options, image, passes=1, rng=None):
        self.calls += passes
        rest = (1 - self.p) / (len(options) - 1)
        return self.label, {l: (self.p if l == self.label else rest)
                            for l, _ in options}

    def yes_no(self, prompt, crop):
        self.calls += 1
        hit = any(f"a {name}?" in prompt for name in self.present)
        return hit, (2.0 if hit else -1.5)


class FakeDetector:
    def detect(self, frame):
        return ([{"label": "calculator", "conf": 0.41, "box": (60, 300, 200, 420)},
                 {"label": "cup", "conf": 0.33, "box": (900, 250, 1000, 400)}], 0.7)


def make_observer(tmp, **kw):
    mem = Memory(os.path.join(tmp, "m.db"))
    obs = Observer(kw.pop("vlm", FakeVLM()), FakeDetector(), mem,
                   rng=random.Random(0), clock=lambda: 0.0,
                   audit_dir=os.path.join(tmp, "audit"),
                   pause_file=os.path.join(tmp, "PAUSE"), **kw)
    return obs, mem


def test_startup_heartbeat_and_verification():
    with tempfile.TemporaryDirectory() as tmp:
        obs, mem = make_observer(tmp)
        assert obs.step(blank(), T0) is not None          # startup
        assert obs.step(blank(), T0 + 30) is None         # nothing new yet
        assert obs.step(blank(), T0 + 61) is not None     # heartbeat
        rows = mem.db.execute("SELECT trigger, activity, vlm_calls FROM observations").fetchall()
        assert [r["trigger"] for r in rows] == ["startup", "heartbeat"]
        assert rows[0]["vlm_calls"] == 3                  # 1 choice + 2 crops
        assert mem.last_seen("calculator") is not None    # verified
        assert mem.last_seen("cup") is None               # detector said cup, VLM said no
        mem.close()


def test_change_cooldown_and_pause():
    with tempfile.TemporaryDirectory() as tmp:
        obs, mem = make_observer(tmp)
        t = T0
        obs.step(blank(), t)                              # startup
        for _ in range(15):                               # you sit down, then stay still
            t += 0.2
            obs.step(with_block(), t)
        n = mem.db.execute("SELECT COUNT(*) FROM observations WHERE trigger='change'").fetchone()[0]
        assert n == 1

        open(os.path.join(tmp, "PAUSE"), "w").close()     # paused: nothing is logged
        before = mem.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        for _ in range(10):
            t += 100
            assert obs.step(blank(), t) is None
        os.remove(os.path.join(tmp, "PAUSE"))
        after = mem.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert before == after
        assert obs.step(blank(), t + 1) is not None       # heartbeat right after resuming
        mem.close()


def test_away_uses_slow_heartbeat_and_uncertain_is_stored():
    with tempfile.TemporaryDirectory() as tmp:
        obs, mem = make_observer(tmp, vlm=FakeVLM(label=settings.NOBODY))
        obs.step(blank(), T0)
        assert obs.step(blank(), T0 + 120) is None        # away: 300 s heartbeat
        assert obs.step(blank(), T0 + 301) is not None
        obs.vlm = FakeVLM(label="reading", p=0.4)          # unsure answer
        obs.step(blank(), T0 + 302 + settings.AWAY_HEARTBEAT_S)
        last = mem.db.execute("SELECT activity, activity_raw FROM observations ORDER BY id DESC").fetchone()
        assert last["activity"] == settings.UNCERTAIN and last["activity_raw"] == "reading"
        mem.close()


def test_audit_thumbnails_only_when_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        obs, mem = make_observer(tmp)
        obs.step(blank(), T0)
        assert not os.path.exists(os.path.join(tmp, "audit"))
        obs.audit_rate = 1.0
        oid = obs.step(blank(), T0 + 61)
        path = mem.db.execute("SELECT audit_path FROM observations WHERE id=?", (oid,)).fetchone()[0]
        assert path and os.path.exists(path)
        mem.forget_day(datetime.fromtimestamp(T0).date().isoformat())
        assert not os.path.exists(path)                   # forgetting deletes the thumbnail
        mem.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} passed")
