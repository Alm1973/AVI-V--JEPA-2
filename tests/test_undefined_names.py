"""No module may reference a name that is never defined.

Several modules here cannot be imported on the Mac -- they need cv2, torch or
the MSI's own settings -- so a NameError in them is invisible until the code
runs on the robot, usually mid-motion. That happened: a refactor of _frame
left run_survey looping `for _ in range(frames)` while the body still used
`frame`. AST parsing was happy, every test passed, and the first survey after
it died on the MSI with `NameError: name 'frame' is not defined`.

pyflakes would catch it and is not installed, so this does the one check that
matters with the standard library's symtable: a name read inside a function
that is not local, not enclosing, not defined at module level and not a
builtin.
"""
import builtins
import symtable
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Modules worth scanning: everything shipped to the MSI.
FILES = sorted(list((ROOT / "avi_bot").rglob("*.py"))
               + list((ROOT / "assistant").glob("*.py")))

MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__spec__",
                  "__package__", "__loader__", "__builtins__", "__path__",
                  "__dict__", "__class__"}


def undefined_names(path):
    src = path.read_text()
    top = symtable.symtable(src, str(path), "exec")
    defined = set(MODULE_DUNDERS) | set(dir(builtins))
    for s in top.get_symbols():
        if s.is_assigned() or s.is_imported() or s.is_namespace():
            defined.add(s.get_name())

    # `global x` inside a function, then assigned, defines x at module level.
    def collect_globals(t):
        for s in t.get_symbols():
            if s.is_declared_global() and s.is_assigned():
                defined.add(s.get_name())
        for c in t.get_children():
            collect_globals(c)
    collect_globals(top)

    found = []

    def walk(t):
        if t.get_type() != "module":
            for s in t.get_symbols():
                if (s.is_referenced() and s.is_global()
                        and not s.is_declared_global()
                        and s.get_name() not in defined):
                    found.append((t.get_name(), t.get_lineno(), s.get_name()))
        for c in t.get_children():
            walk(c)
    walk(top)
    return found


class NoUndefinedNames(unittest.TestCase):
    def test_every_shipped_module(self):
        problems = []
        for f in FILES:
            try:
                for scope, line, name in undefined_names(f):
                    problems.append(
                        f"{f.relative_to(ROOT)}:{line} in {scope}(): "
                        f"'{name}' is never defined")
            except SyntaxError as exc:
                problems.append(f"{f.relative_to(ROOT)}: {exc}")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_it_would_have_caught_the_survey_bug(self):
        import tempfile
        bad = ("def run(latest, frames):\n"
               "    for _ in range(frames):\n"
               "        use(frame)\n"
               "def use(x):\n"
               "    return x\n")
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as fh:
            fh.write(bad)
        got = undefined_names(Path(fh.name))
        self.assertIn("frame", [n for _, _, n in got])
