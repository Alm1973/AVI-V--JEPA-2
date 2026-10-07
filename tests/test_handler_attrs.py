"""The HTTP Handler must not reach for the Arm's attributes on `self`.

`self.tune` in the MJPEG loop raised after the first frame of every stream,
so the camera feed looked frozen to everyone, for a day, with the log full of
tracebacks nobody read. The Handler gets the arm through the closure as `arm`;
`self` is the request handler. Read from source: server.py needs the MSI's
modules to import.
"""
import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "avi_bot/dashboard/server.py"

# Attributes that live on Arm, never on the request handler.
ARM_ONLY = {"tune", "engine", "target", "roommap", "presence", "desk",
            "latents", "mood", "config", "detector", "paused", "lock"}


class Handler(unittest.TestCase):
    def test_no_arm_attributes_on_self(self):
        tree = ast.parse(SRC.read_text())
        bad = []
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef) and cls.name == "Handler":
                for node in ast.walk(cls):
                    if (isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id == "self"
                            and node.attr in ARM_ONLY):
                        bad.append(f"line {node.lineno}: self.{node.attr}")
        self.assertEqual(bad, [], "use `arm.` inside Handler")


if __name__ == "__main__":
    unittest.main()
