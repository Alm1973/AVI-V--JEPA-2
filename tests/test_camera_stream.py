"""The parts of the camera stream that can be tested without a camera.

Worth testing precisely because the MSI is usually asleep: a bug here costs a
wake cycle to find. The frame fan-out and the bind-address refusal are both
pure stdlib, so they can be checked from anywhere.
"""
import sys
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_stream():
    for name in ("cv2", "settings", "change", "observer", "camera_stream"):
        sys.modules.pop(name, None)
    cv2 = types.ModuleType("cv2")
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.INTER_AREA = 3
    cv2.CAP_DSHOW = 700
    cv2.CAP_ANY = 0
    for fn in ("imencode", "resize", "VideoCapture"):
        setattr(cv2, fn, lambda *a, **k: None)
    sys.modules["cv2"] = cv2
    settings = types.ModuleType("settings")
    settings.CAMERA_INDEX = 1
    sys.modules["settings"] = settings
    observer = types.ModuleType("observer")
    observer.open_camera = lambda i: None
    observer.read_fresh = lambda cap, drain=2: (False, None)
    sys.modules["observer"] = observer
    sys.path.insert(0, str(ROOT / "assistant"))
    import importlib
    return importlib.import_module("camera_stream")


class TestLatestFrame(unittest.TestCase):
    def setUp(self):
        self.cs = load_stream()

    def test_a_waiting_viewer_is_woken_by_a_new_frame(self):
        latest = self.cs.Latest()
        got = []

        def viewer():
            got.append(latest.get_after(0, timeout=2.0))

        t = threading.Thread(target=viewer)
        t.start()
        time.sleep(0.05)
        latest.put(b"jpeg-one")
        t.join(2.0)
        self.assertEqual(got, [(b"jpeg-one", 1)])

    def test_two_viewers_both_get_the_same_frame(self):
        # One capture thread, many viewers. If a viewer ever opened the device
        # itself the second one would fail: DirectShow is exclusive.
        latest = self.cs.Latest()
        got = []
        lock = threading.Lock()

        def viewer():
            r = latest.get_after(0, timeout=2.0)
            with lock:
                got.append(r)

        ts = [threading.Thread(target=viewer) for _ in range(2)]
        for t in ts:
            t.start()
        time.sleep(0.05)
        latest.put(b"shared")
        for t in ts:
            t.join(2.0)
        self.assertEqual(got, [(b"shared", 1), (b"shared", 1)])

    def test_viewer_does_not_spin_on_an_unchanged_frame(self):
        latest = self.cs.Latest()
        latest.put(b"one")
        t0 = time.time()
        jpeg, seq = latest.get_after(1, timeout=0.3)
        self.assertIsNone(jpeg, "returned a frame it had already sent")
        self.assertGreaterEqual(time.time() - t0, 0.25, "did not actually wait")

    def test_stop_releases_a_waiting_viewer(self):
        latest = self.cs.Latest()
        got = []
        t = threading.Thread(target=lambda: got.append(
            latest.get_after(0, timeout=5.0)))
        t.start()
        time.sleep(0.05)
        latest.stop.set()
        with latest.lock:
            latest.lock.notify_all()
        t.join(2.0)
        self.assertFalse(t.is_alive(), "a viewer outlived stop()")
        self.assertEqual(got[0][0], None)


class TestBindRefusal(unittest.TestCase):
    def setUp(self):
        self.cs = load_stream()

    def test_it_refuses_to_bind_to_everything_when_tailscale_is_missing(self):
        """The camera faces his room. Falling back to 0.0.0.0 would put it on
        the whole home LAN, which is exactly the mistake worth preventing."""
        self.cs.tailscale_ip = lambda: None
        argv = sys.argv
        sys.argv = ["camera_stream.py"]
        try:
            rc = self.cs.main()
        finally:
            sys.argv = argv
        self.assertEqual(rc, 2)

    def test_an_explicit_bind_is_still_honoured(self):
        # Deliberately passing --bind 0.0.0.0 is a choice, not an accident.
        self.cs.tailscale_ip = lambda: None
        argv = sys.argv
        sys.argv = ["camera_stream.py", "--bind", "127.0.0.1", "--minutes", "0"]
        try:
            rc = self.cs.main()     # deadline already past: starts and exits
        finally:
            sys.argv = argv
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
