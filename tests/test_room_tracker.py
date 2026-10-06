"""The tracker's camera handoff and its unattended failure handling.

The tracker runs for days with nobody watching, and it passes the camera back
and forth with the logger process. On 2026-10-04 a single failed handoff ended
a whole evening's run: open_camera raised, nothing caught it, and the task
exited rc=1 after 20 minutes. These tests are about that failure mode.

The Mac has no OpenCV, which is the point of the stubs: this logic is worth
testing without a GPU, a camera, or the MSI being awake.
"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# What this file replaces in sys.modules. NOT numpy, which it used to pop:
# numpy is a real package in the venv, and removing it from sys.modules while
# its C core stays loaded means the next import re-runs numpy/__init__.py over
# half-initialised state. Every real-numpy test that sorted after this one in
# discover order then crashed -- `import numpy` recursed without end, and a
# later call segfaulted inside np.triu_indices. It stayed hidden because the
# only numpy-heavy tests (depth_probe, latent_probe) sort BEFORE "room".
STUBBED = ("cv2", "settings", "change", "observer", "room_tracker")


def load_tracker(test=None):
    """Import assistant/room_tracker.py with cv2 and its siblings stubbed.

    Pass the TestCase and the stubs are removed again when it finishes. They
    used to persist for the rest of the run, so a fake `settings` with five
    attributes was what any later test got if it imported settings.
    """
    saved = {n: sys.modules.get(n) for n in STUBBED}
    if test is not None:
        def restore():
            for n, m in saved.items():
                if m is None:
                    sys.modules.pop(n, None)
                else:
                    sys.modules[n] = m
        test.addCleanup(restore)
    for name in STUBBED:
        sys.modules.pop(name, None)

    cv2 = types.ModuleType("cv2")
    cv2.CAP_DSHOW = 700
    cv2.CAP_ANY = 0
    cv2.COLOR_BGR2GRAY = 6
    cv2.INTER_AREA = 3
    for fn in ("VideoCapture", "cvtColor", "resize", "absdiff", "imwrite",
               "imshow", "waitKey", "destroyAllWindows", "putText"):
        setattr(cv2, fn, lambda *a, **k: None)
    sys.modules["cv2"] = cv2

    settings = types.ModuleType("settings")
    settings.CAMERA_INDEX = 1
    settings.AUDIT_RATE = 0.0
    settings.CHANGE_SIZE = (160, 90)
    settings.CHANGE_PIXEL_DIFF = 25
    settings.CHANGE_FRACTION = 0.03
    sys.modules["settings"] = settings

    change = types.ModuleType("change")

    class ChangeDetector:
        def _prep(self, frame):
            return frame

        def _fraction(self, a, b):
            return 0.0

    change.ChangeDetector = ChangeDetector
    sys.modules["change"] = change

    observer = types.ModuleType("observer")
    observer.open_camera = lambda index: None
    sys.modules["observer"] = observer

    sys.path.insert(0, str(ROOT / "assistant"))
    import importlib
    return importlib.import_module("room_tracker")


class Args:
    camera = 1


class TestItLeavesTheInterpreterAlone(unittest.TestCase):
    def test_numpy_is_untouched(self):
        import numpy
        before = sys.modules["numpy"]
        load_tracker(self)
        self.assertIs(sys.modules["numpy"], before)
        self.assertIs(numpy, before)

    def test_its_stubs_are_gone_afterwards(self):
        real = sys.modules.get("settings")

        class Probe(unittest.TestCase):
            def runTest(self):
                load_tracker(self)
                assert sys.modules["settings"].__dict__.get("CAMERA_INDEX") == 1

        unittest.TextTestRunner(stream=open("/dev/null", "w")).run(Probe())
        self.assertIs(sys.modules.get("settings"), real)


class TestAcquireCamera(unittest.TestCase):
    def setUp(self):
        self.rt = load_tracker(self)
        self.slept = []
        self.rt.time.sleep = self.slept.append
        self.logged = []
        self.rt.log = self.logged.append

    def _open_failing(self, times, exc=RuntimeError):
        """An open_camera that fails `times` times, then succeeds."""
        state = {"n": 0}

        def opener(index):
            state["n"] += 1
            if state["n"] <= times:
                raise exc("camera still returns blank frames after 8 s")
            return f"cap-after-{state['n']}"
        return opener, state

    def test_succeeds_first_try(self):
        self.rt.open_camera = lambda i: "cap"
        self.assertEqual(self.rt.acquire_camera(Args()), "cap")
        self.assertEqual(self.slept, [], "should not sleep when it works")

    def test_recovers_from_the_failure_that_killed_the_run(self):
        opener, state = self._open_failing(3)
        self.rt.open_camera = opener
        self.assertEqual(self.rt.acquire_camera(Args()), "cap-after-4")
        self.assertEqual(state["n"], 4)

    def test_backoff_grows_and_is_capped(self):
        opener, _ = self._open_failing(5)
        self.rt.open_camera = opener
        self.rt.acquire_camera(Args(), attempts=6)
        self.assertEqual(self.slept, [2.0, 4.0, 8.0, 16.0, 30.0])

    def test_gives_up_after_the_last_attempt_and_reraises(self):
        opener, state = self._open_failing(99)
        self.rt.open_camera = opener
        with self.assertRaises(RuntimeError):
            self.rt.acquire_camera(Args(), attempts=4)
        # Exactly `attempts` tries, and no sleep after the final one: sleeping
        # then would delay the caller's own backoff for no reason.
        self.assertEqual(state["n"], 4)
        self.assertEqual(len(self.slept), 3)


class TestConstants(unittest.TestCase):
    def setUp(self):
        self.rt = load_tracker(self)

    def test_handoff_delay_is_longer_than_the_old_one_five_seconds(self):
        # 1.5 s was what was in place when the handoff failed.
        self.assertGreater(self.rt.HANDOFF_S, 1.5)

    def test_failure_limit_is_bounded(self):
        self.assertGreaterEqual(self.rt.MAX_CONSECUTIVE_FAILS, 3)
        self.assertLessEqual(self.rt.MAX_CONSECUTIVE_FAILS, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
