"""The dashboard page lives in a bytes literal inside server.py.

A JS "\\u2713" written there with ONE backslash is an invalid escape in
Python: a SyntaxWarning today, a SyntaxError in a future Python, and the
whole dashboard fails to start on the MSI. server.py cannot be imported on
the Mac (cv2, torch), so this compiles it with warnings as errors instead.
"""
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PageSource(unittest.TestCase):
    def test_server_compiles_without_escape_warnings(self):
        src = (ROOT / "avi_bot" / "dashboard" / "server.py").read_text()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(src, "server.py", "exec")



class EndsOnTime(unittest.TestCase):
    """handle_request() blocks until a request arrives; with no timeout an
    ended run held the camera and serial port until someone next opened
    the page (dashboard, 2026-10-09: 23:20 -> 00:17)."""

    def test_every_serve_loop_wakes_to_check_its_deadline(self):
        import re
        root = Path(__file__).resolve().parents[1]
        for src in (root / "avi_bot" / "dashboard" / "server.py",
                    root / "assistant" / "camera_stream.py",
                    root / "assistant" / "teleop.py",
                    root / "assistant" / "calibrate.py"):
            text = src.read_text()
            if "handle_request()" not in text:
                continue
            with self.subTest(src.name):
                m = re.search(r"srv\.timeout = ([0-9.]+)", text)
                self.assertIsNotNone(m, f"{src.name}: no srv.timeout")
                self.assertLessEqual(float(m.group(1)), 5.0)
                self.assertLess(text.index("srv.timeout"),
                                text.index("srv.handle_request()"))

if __name__ == "__main__":
    unittest.main()
