"""Launch day must start from zero.

AVI is meant to have a birth: Shaurya wakes it up and it begins learning from
that moment. A robot whose stores already hold weeks of development data was
never born, it was restored from a backup, and the growth he wants to watch
would have happened off camera before he looked.

The guarantee is that everything learned in development lands in a separate
file, so launch day starts empty by DEFAULT rather than by anyone remembering
to delete something the night before. That rule lived in two unrelated files as
an unexamined default string and nothing checked either of them, which is
precisely the kind of promise that is discovered to be broken on the one day it
matters.

These read the source rather than importing it: both modules pull in cv2,
torch and the MSI's own settings, none of which exist on the Mac. An AST read
checks the real values, not a substring.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Things learned about Shaurya or his room. Calibration is NOT here on purpose:
# a measurement about the hardware is not a memory, and poses and the measured
# degrees-per-frame constants are meant to carry over.
LEARNED_STORES = {
    "assistant/latent_log.py": ("PROFILES", "latents-dev.db", "latents.db"),
    "avi_bot/dashboard/server.py": ("ROOMMAP_PROFILES", "roommap-dev.json",
                                    "roommap.json"),
}


def assignments(path, name):
    """Every value assigned to `name` anywhere in a module, as Python objects."""
    tree = ast.parse((ROOT / path).read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                try:
                    out.append(ast.literal_eval(node.value))
                except ValueError:
                    pass
    return out


def profile_defaults(path):
    """The fallback of every os.environ.get("AVI_PROFILE", ...) in the file."""
    tree = ast.parse((ROOT / path).read_text())
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "AVI_PROFILE"):
            out.append(node.args[1].value)
    return out


class SeparateStores(unittest.TestCase):
    def test_every_learned_store_has_a_dev_and_a_live_file(self):
        for path, (const, dev, live) in LEARNED_STORES.items():
            with self.subTest(path):
                found = assignments(path, const)
                self.assertTrue(found, f"{path} has no {const}")
                self.assertEqual(found[0], {"dev": dev, "live": live})

    def test_the_two_files_are_never_the_same(self):
        """A dev store that writes the live filename is the whole bug."""
        for path, (const, dev, live) in LEARNED_STORES.items():
            with self.subTest(path):
                self.assertNotEqual(dev, live)

    def test_dev_is_the_default_everywhere(self):
        """Live has to be asked for. If production is the fallback then every
        forgotten flag writes development data into AVI's birth."""
        for path in LEARNED_STORES:
            with self.subTest(path):
                for default in profile_defaults(path):
                    self.assertEqual(default, "dev",
                                     f"{path} falls back to {default!r}")

    def test_the_latent_logger_writes_the_dev_db_unasked(self):
        """LATENT_DB is what a caller gets when it passes no profile at all."""
        tree = ast.parse((ROOT / "assistant" / "latent_log.py").read_text())
        keys = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "LATENT_DB"
                       for t in node.targets):
                continue
            v = node.value
            if isinstance(v, ast.Constant):
                keys.append(v.value)
            elif (isinstance(v, ast.Subscript)
                  and isinstance(v.value, ast.Name)
                  and v.value.id == "PROFILES"):
                keys.append(v.slice.value)
        self.assertEqual(keys, ["dev"],
                         f"LATENT_DB resolves to {keys}, not the dev store")

    def test_the_latent_cli_defaults_to_dev(self):
        tree = ast.parse((ROOT / "assistant" / "latent_log.py").read_text())
        found = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"
                    and any(isinstance(a, ast.Constant) and a.value == "--profile"
                            for a in node.args)):
                found += [kw.value.value for kw in node.keywords
                          if kw.arg == "default"]
        self.assertEqual(found, ["dev"], "--profile does not default to dev")
