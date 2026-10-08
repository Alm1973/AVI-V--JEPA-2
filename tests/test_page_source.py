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


if __name__ == "__main__":
    unittest.main()
