"""The senses' event rules, without a camera. avi_bot/senses/events.py.

Each test feeds a scripted sequence of observations and checks which events
come out and when. The rules are the part that decides what AVI believes
happened, so they are the part worth pinning down before the MSI ever runs.
"""
import tempfile
import unittest
from pathlib import Path

from avi_bot.senses import events as E
from avi_bot.senses.store import Store, pack, unpack

FPS = 5.0
BOX = (0.5, 0.5, 0.3, 0.6)


def face(yaw=0.0, jaw=0.0, blink=0.0, box=(0.5, 0.3, 0.1, 0.1)):
    return E.Face(box, yaw, 0.0, jaw, blink)


def run(senses, seconds, t0=0.0, bright=120.0, bodies=None, faces=None, hands=None, fps=FPS):
    """Feed `seconds` of identical frames. Returns (events, end time)."""
    out, n = [], int(round(seconds * fps))
    for i in range(n):
        ts = t0 + i / fps
        out += senses.update(E.Obs(ts, bright,
                                   bodies=None if bodies is None else list(bodies),
                                   faces=None if faces is None else list(faces),
                                   hands=None if hands is None else list(hands)))
    return out, t0 + n / fps


def kinds(evs):
    return [e.kind for e in evs]


class Presence(unittest.TestCase):
    def test_arrives_after_continuous_sighting_not_one_frame(self):
        s = E.Senses()
        evs, t = run(s, 1.0, bodies=[E.Body(BOX, False)], faces=[face(yaw=60, blink=0)])
        self.assertNotIn("arrived", kinds(evs))
        evs, t = run(s, 3.0, t0=t, bodies=[E.Body(BOX, False)], faces=[face(yaw=60)])
        self.assertEqual(kinds(evs).count("arrived"), 1)
        self.assertEqual(evs[kinds(evs).index("arrived")].ts, 0.0)   # stamped at the start

    def test_leaves_only_after_leave_s_and_is_stamped_at_last_sighting(self):
        s = E.Senses()
        _, t = run(s, 10, bodies=[E.Body(BOX, False)], faces=[face(yaw=60)])
        last = t - 1 / FPS
        evs, t = run(s, E.LEAVE_S - 5, t0=t, bodies=[], faces=[])
        self.assertNotIn("left", kinds(evs))
        evs, t = run(s, 10, t0=t, bodies=[], faces=[])
        left = [e for e in evs if e.kind == "left"]
        self.assertEqual(len(left), 1)
        self.assertAlmostEqual(left[0].ts, last)

    def test_a_frame_where_body_did_not_run_is_not_absence(self):
        s = E.Senses()
        _, t = run(s, 10, bodies=[E.Body(BOX, False)], faces=[face(yaw=60)])
        evs, _ = run(s, E.LEAVE_S + 30, t0=t, hands=[])      # only hands ran
        self.assertNotIn("left", kinds(evs))


class Decoys(unittest.TestCase):
    def test_a_still_figure_is_learned_and_then_ignored(self):
        s = E.Senses()
        evs, t = run(s, E.DECOY_STILL_S + 5, bodies=[E.Body(BOX, False)], faces=[], fps=1)
        self.assertIn("learned_decoy", kinds(evs))
        # it had "arrived" before it was learned; now it no longer keeps him present
        evs, _ = run(s, E.LEAVE_S + 10, t0=t, bodies=[E.Body(BOX, False)], faces=[], fps=1)
        self.assertIn("left", kinds(evs))

    def test_a_blinking_face_is_alive_and_never_a_decoy(self):
        s = E.Senses()
        t = 0.0
        evs = []
        while t < E.DECOY_STILL_S + 20:
            blink = 1.0 if int(t * FPS) % 20 == 0 else 0.0     # a short blink every 4 s
            evs += s.update(E.Obs(t, 120.0, bodies=[E.Body(BOX, False)], faces=[face(blink=blink)]))
            t += 1 / FPS
        self.assertNotIn("learned_decoy", kinds(evs))

    def test_lying_still_is_sleep_not_a_poster(self):
        s = E.Senses()
        evs, _ = run(s, E.DECOY_STILL_S + 20, bodies=[E.Body(BOX, True)], faces=[], fps=1)
        self.assertNotIn("learned_decoy", kinds(evs))

    def test_a_decoy_that_keeps_moving_is_forgotten(self):
        d = E.Decoys(known=[{"box": BOX}])
        moved = (BOX[0] + 0.05, BOX[1], BOX[2], BOX[3])
        for _ in range(E.DECOY_FORGET_AFTER):
            self.assertFalse(d.is_decoy(moved))
        self.assertEqual(d.known, [])


class Face(unittest.TestCase):
    def test_look_must_be_held_and_cools_down(self):
        s = E.Senses()
        evs, t = run(s, 0.6, faces=[face(yaw=5)])
        self.assertNotIn("looked_at_avi", kinds(evs))
        evs, t = run(s, 5, t0=t, faces=[face(yaw=5)])
        self.assertEqual(kinds(evs).count("looked_at_avi"), 1)    # not once per frame
        evs, _ = run(s, 5, t0=t, faces=[face(yaw=45)])
        self.assertNotIn("looked_at_avi", kinds(evs))

    def test_yawn(self):
        s = E.Senses()
        evs, _ = run(s, 3, faces=[face(yaw=60, jaw=0.8)])
        self.assertEqual(kinds(evs).count("yawn"), 1)

    def test_eyes_closed_then_opened_with_duration(self):
        s = E.Senses()
        evs, t = run(s, 8, faces=[face(yaw=60, blink=0.9)])
        self.assertEqual(kinds(evs).count("eyes_closed"), 1)
        evs, _ = run(s, 1, t0=t, faces=[face(yaw=60, blink=0.0)])
        opened = [e for e in evs if e.kind == "eyes_opened"]
        self.assertEqual(len(opened), 1)
        self.assertGreaterEqual(opened[0].detail["closed_s"], 7.5)

    def test_others_present(self):
        s = E.Senses()
        evs, _ = run(s, 4, faces=[face(yaw=60), face(yaw=60, box=(0.2, 0.3, 0.05, 0.05))])
        self.assertEqual(kinds(evs).count("others_present"), 1)


class Gestures(unittest.TestCase):
    def test_needs_votes_and_cools_down(self):
        s = E.Senses()
        thumb = [E.Hand("Thumb_Up", 0.9, "Right")]
        evs, t = run(s, 3 / 15, hands=thumb, fps=15)
        self.assertNotIn("gesture", kinds(evs))
        evs, t = run(s, 2.0, t0=t, hands=thumb, fps=15)
        g = [e for e in evs if e.kind == "gesture"]
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0].detail["name"], "Thumb_Up")

    def test_one_long_hold_is_one_gesture_and_a_second_hold_is_another(self):
        s = E.Senses()
        thumb = [E.Hand("Thumb_Up", 0.9, "Right")]
        evs, t = run(s, 10, hands=thumb, fps=15)
        self.assertEqual(kinds(evs).count("gesture"), 1)
        _, t = run(s, 1, t0=t, hands=[], fps=15)
        evs, _ = run(s, 1, t0=t, hands=thumb, fps=15)
        self.assertEqual(kinds(evs).count("gesture"), 1)

    def test_low_score_and_none_are_ignored(self):
        s = E.Senses()
        evs, _ = run(s, 2, hands=[E.Hand("Thumb_Up", 0.3)], fps=15)
        evs2, _ = run(s, 2, hands=[E.Hand("None", 0.99)], fps=15)
        self.assertNotIn("gesture", kinds(evs + evs2))


class Lights(unittest.TestCase):
    def test_default_threshold_with_hold(self):
        s = E.Senses()
        run(s, 5, bright=150)
        evs, t = run(s, E.LIGHTS_HOLD_S - 2, t0=5, bright=10)
        self.assertNotIn("lights_off", kinds(evs))
        evs, _ = run(s, 5, t0=t, bright=10)
        off = [e for e in evs if e.kind == "lights_off"]
        self.assertEqual(len(off), 1)
        self.assertFalse(off[0].detail["learned"])

    def test_threshold_is_learned_from_its_own_history(self):
        hist = [0] * 256
        hist[70] = 400       # this room's "dark" is brighter than the default
        hist[160] = 400
        lights = E.Lights(hist)
        thr, learned = lights.threshold()
        self.assertTrue(learned)
        self.assertTrue(70 < thr < 160)

    def test_one_hump_is_not_enough_to_learn(self):
        hist = [0] * 256
        hist[120] = 1000
        self.assertEqual(E.Lights(hist).threshold(), (E.LIGHTS_DEFAULT, False))


class Samples(unittest.TestCase):
    def test_a_row_every_ten_seconds_even_when_nothing_happens(self):
        s = E.Senses()
        rows = []
        for i in range(int(35 * FPS)):
            s.update(E.Obs(i / FPS, 100.0, bodies=[], faces=[]))
            r = s.sample(i / FPS)
            if r:
                rows.append(r)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["present"], 0)
        self.assertAlmostEqual(rows[0]["brightness"], 100.0)


class StoreRoundTrip(unittest.TestCase):
    def test_events_samples_vectors_and_learned_state(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "s.db", clock=lambda: 5.0)
            st.event(E.Event(1.0, "arrived"))
            st.event(E.Event(2.0, "gesture", {"name": "Victory"}))
            st.sample({"ts": 3.0, "present": 1, "brightness": 99.0})
            st.vector(3.0, "hand", [0.25, -1.0])
            st.save("decoys", [{"box": [0.1, 0.2, 0.3, 0.4]}])
            st.commit()
            self.assertEqual(st.events(kind="gesture"), [(2.0, "gesture", {"name": "Victory"})])
            self.assertEqual(st.counts(), {"events": 2, "samples": 1, "vectors": 1})
            self.assertEqual(st.load("decoys"), [{"box": [0.1, 0.2, 0.3, 0.4]}])
            self.assertIsNone(st.load("missing"))
            st.close()
        self.assertEqual(unpack(pack([0.5, 2.0])), [0.5, 2.0])


if __name__ == "__main__":
    unittest.main()
