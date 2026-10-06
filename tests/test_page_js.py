"""The served page's JavaScript must parse.

Twice now a page has been shipped whose script died before its first update,
leaving the UI frozen on its placeholder text while every server-side check
said the session was healthy. Both times the cause was the same trap: PAGE is a
Python bytes literal, so an escape written for JavaScript is eaten by Python
first. `"\\n"` in the source becomes a real newline byte in the served HTML and
breaks the JS string it was inside.

Nothing here talks to the MSI. It extracts the page exactly as the server would
send it and checks two things: that it parses, and that it does not reach for
elements the markup no longer has.
"""
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCES = [ROOT / "assistant" / "teleop.py",
           ROOT / "assistant" / "camera_stream.py",
           ROOT / "assistant" / "calibrate.py"]


def served_pages():
    """(name, html) for every PAGE literal, decoded the way the wire sees it."""
    out = []
    for src in SOURCES:
        if not src.exists():
            continue
        text = src.read_text()
        for m in re.finditer(r'PAGE = b"""(.*?)"""', text, re.S):
            out.append((src.name, m.group(1).encode().decode("unicode_escape")))
    return out


class TestServedPages(unittest.TestCase):
    def setUp(self):
        self.pages = served_pages()
        self.assertTrue(self.pages, "no PAGE literal found to check")

    def test_every_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed; cannot parse-check the page")
        for name, html in self.pages:
            for i, chunk in enumerate(html.split("<script>")[1:]):
                js = chunk.split("</script>", 1)[0]
                with self.subTest(page=name, script=i):
                    tmp = ROOT / f".page_check_{name}_{i}.js"
                    tmp.write_text(js)
                    try:
                        r = subprocess.run([node, "--check", str(tmp)],
                                           capture_output=True, text=True)
                    finally:
                        tmp.unlink(missing_ok=True)
                    self.assertEqual(r.returncode, 0,
                                     f"{name} script {i} does not parse:\n"
                                     f"{r.stderr[:600]}")

    def test_no_script_reaches_for_an_element_that_is_not_there(self):
        """A null from getElementById throws, and the throw kills the rest of
        the callback it was in -- which is how a stale id froze the whole UI
        while the server looked perfectly healthy."""
        for name, html in self.pages:
            ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
            wanted = set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)',
                                    html))
            missing = sorted(wanted - ids)
            self.assertEqual(missing, [],
                             f"{name} looks up ids that the markup does not "
                             f"define: {missing}")

    def test_no_single_escaped_whitespace_in_a_page_literal(self):
        """The precise form of the trap, checked at source level.

        PAGE is a Python bytes literal, so a `\\n` written for JavaScript is
        consumed by Python and emitted as a real newline byte -- which breaks
        whatever JS string it was inside. A genuine line break in the page is
        written as an actual line break, never as an escape, so any
        single-escaped whitespace in one of these literals is a mistake.

        An earlier version of this test tried to spot the damage downstream by
        counting quotes per line, and flagged an apostrophe in a `//` comment.
        Checking the cause instead of the symptom has no such ambiguity.
        """
        for src in SOURCES:
            if not src.exists():
                continue
            text = src.read_text()
            for m in re.finditer(r'PAGE = b"""(.*?)"""', text, re.S):
                literal = m.group(1)
                for esc, name in ((r"\\n", "newline"), (r"\\t", "tab"),
                                  (r"\\r", "carriage return")):
                    hits = re.findall(r"(?<!\\)" + esc, literal)
                    self.assertEqual(
                        hits, [],
                        f"{src.name}: a single-escaped {name} in the PAGE "
                        f"literal. Python will turn it into a real character "
                        f"in the served page; double it to reach JavaScript.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
