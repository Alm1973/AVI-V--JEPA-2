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
           ROOT / "assistant" / "calibrate.py",
           ROOT / "avi_bot" / "dashboard" / "server.py"]


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

    def test_the_chat_log_shows_what_the_camera_said(self):
        """Runs the served page's own didLine() in node on the shapes POST
        /ask returns: a judged move, a stepped one, a look the camera
        confirmed or did not, and a move_joints."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        html = dict(self.pages)["server.py"]
        js = html.split("<script>")[1].split("</script>", 1)[0]
        m = re.search(r"function esc\(t\)\{.*?\n\}\nfunction didLine\(d\)"
                      r"\{.*?\n\}\n", js, re.S)
        self.assertIsNotNone(m, "didLine not found in the page")
        cases = [
            {"tool": "nudge", "args": {"direction": "left", "degrees": 30},
             "ok": True, "verdict": {"judged": True, "agrees": True,
                                     "votes": {"jepa": "left"},
                                     "steps": 2}},
            {"tool": "look_at", "args": {"target": "laptop"}, "ok": True,
             "in_view": True},
            {"tool": "look_at", "args": {"target": "mug"}, "ok": True,
             "in_view": False},
            {"tool": "move_joints", "args": {"base_yaw": 60}, "ok": True},
            {"tool": "set_timer", "args": {"seconds": 600}, "ok": True},
            {"tool": "remember", "args": {"text": "math test Monday"},
             "ok": True},
        ]
        prog = m.group(0) + "\nconsole.log(JSON.stringify(" + \
            __import__("json").dumps(cases) + ".map(didLine)));"
        tmp = ROOT / ".page_didline_check.js"
        tmp.write_text(prog)
        try:
            r = subprocess.run([node, str(tmp)], capture_output=True,
                               text=True)
        finally:
            tmp.unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0, r.stderr[:600])
        out = __import__("json").loads(r.stdout)
        self.assertEqual(out[0]["text"], "nudge left 30\u00b0 \u2713 camera "
                                         "saw it (jepa, 2 steps)")
        self.assertEqual(out[1], {"bad": False, "text": "look at laptop "
                                                        "\u2713 camera found it"})
        self.assertEqual(out[2], {"bad": True, "text": "look at mug "
                                                       "\u2717 not in view"})
        self.assertEqual(out[3]["text"], "move joints base_yaw 60")
        self.assertEqual(out[4]["text"], "set timer 10 min")
        self.assertEqual(out[5]["text"], "remember: math test Monday")

    def test_the_mic_button_turns_speech_into_an_ask(self):
        """Runs the page's micSetup()/listen() in node with a fake
        browser: on https the words land in the ask box and ask() runs; on
        plain http it says why not; with no speech API the button hides."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        html = dict(self.pages)["server.py"]
        js = html.split("<script>")[1].split("</script>", 1)[0]
        m = re.search(r"const SR = .*?\nfunction listen\(\)\{.*?\n\}\n",
                      js, re.S)
        self.assertIsNotNone(m, "mic code not found in the page")
        harness = '''
const els = {mic: {style: {}, title: "", classList: {add(){}, remove(){}}},
             askin: {value: ""}};
let said = [], asked = [];
const document = {getElementById: id => els[id]};
function msg(t){ said.push(t); }
function ask(){ asked.push(els.askin.value); }
function run(secure, hasSR){
  const window = {isSecureContext: secure};
  if (hasSR) window.webkitSpeechRecognition = class {
    start(){ this.onresult({results: [[{transcript: "look left"}]]});
             this.onend(); } };
  said = []; asked = []; els.askin.value = ""; els.mic.style = {};
  eval(CODE.replace("const SR", "var SR"));
  micSetup(); listen();
  return {said, asked, hidden: els.mic.style.display === "none"};
}
console.log(JSON.stringify([run(true, true), run(false, true),
                            run(true, false)]));
'''
        prog = "const CODE = " + __import__("json").dumps(m.group(0)) + \
            ";\n" + harness
        tmp = ROOT / ".page_mic_check.js"
        tmp.write_text(prog)
        try:
            r = subprocess.run([node, str(tmp)], capture_output=True,
                               text=True)
        finally:
            tmp.unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0, r.stderr[:600])
        https, http, none = __import__("json").loads(r.stdout)
        self.assertEqual(https["asked"], ["look left"])
        self.assertEqual(http["asked"], [])
        self.assertIn("https", http["said"][0])
        self.assertTrue(none["hidden"])

    def test_announcements_join_the_chat_once(self):
        """A timer going off with the speaker off never reached the page.
        Runs the page's heardFrom(): no replay on load, each new one once."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        html = dict(self.pages)["server.py"]
        js = html.split("<script>")[1].split("</script>", 1)[0]
        m = re.search(r"const HEARD = new Set\(\);.*?\nfunction heardFrom"
                      r"\(list\)\{.*?\n\}\n", js, re.S)
        self.assertIsNotNone(m, "heardFrom not found in the page")
        prog = ("const CHAT = []; let painted = 0;\n"
                "function paintChat(){ painted++; }\n" + m.group(0) + '''
const old = {at: 1, kind: "timer", text: "Your 1 minute timer is done."};
const fresh = {at: 2, kind: "rundown", text: "Today: Chemistry test."};
heardFrom([old]);
heardFrom([old, fresh]);
heardFrom([old, fresh]);
console.log(JSON.stringify({chat: CHAT, painted}));
''')
        tmp = ROOT / ".page_heard_check.js"
        tmp.write_text(prog)
        try:
            r = subprocess.run([node, str(tmp)], capture_output=True,
                               text=True)
        finally:
            tmp.unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0, r.stderr[:600])
        out = __import__("json").loads(r.stdout)
        self.assertEqual([e["avi"] for e in out["chat"]],
                         ["Today: Chemistry test."])
        self.assertEqual(out["chat"][0]["note"], "rundown")
        self.assertEqual(out["painted"], 1)

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
