"""The .env loader. Small, and the failure it prevents is silent.

providers.Chain reads os.environ, and nothing was populating it from .env. The
key worked from the Mac because it had been exported by hand; on the MSI the
chain would have said 'groq: no key', skipped to the local fallback, and
looked exactly like a rate limit.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot import env as env_mod                            # noqa: E402


class Parsing(unittest.TestCase):
    def test_basic_lines(self):
        got = env_mod.parse("A=1\nB=two\n")
        self.assertEqual(got, {"A": "1", "B": "two"})

    def test_comments_and_blanks(self):
        got = env_mod.parse("# a note\n\nA=1\n   \n#B=2\n")
        self.assertEqual(got, {"A": "1"})

    def test_export_prefix(self):
        self.assertEqual(env_mod.parse("export A=1\n"), {"A": "1"})

    def test_quotes_are_stripped_once(self):
        got = env_mod.parse("A='x'\nB=\"y\"\nC=\"he said \"hi\"\"\n")
        self.assertEqual(got["A"], "x")
        self.assertEqual(got["B"], "y")
        # Only a matched outer pair comes off; inner quotes are part of it.
        self.assertEqual(got["C"], 'he said "hi"')

    def test_a_value_containing_equals_survives(self):
        """Base64 keys end in '=' padding."""
        self.assertEqual(env_mod.parse("K=abc=def==\n")["K"], "abc=def==")

    def test_blank_values_are_kept_as_blank_by_parse(self):
        self.assertEqual(env_mod.parse("A=\n"), {"A": ""})


class Loading(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".env"

    def write(self, text):
        self.path.write_text(text)
        return self.path

    def test_values_reach_the_environment(self):
        envd = {}
        applied = env_mod.load(self.write("GROQ_API_KEY=gsk_x\n"), environ=envd)
        self.assertEqual(envd["GROQ_API_KEY"], "gsk_x")
        self.assertEqual(applied, ["GROQ_API_KEY"])

    def test_it_returns_names_never_values(self):
        """So the result can be logged."""
        envd = {}
        applied = env_mod.load(self.write("GROQ_API_KEY=gsk_secret\n"),
                               environ=envd)
        self.assertNotIn("gsk_secret", " ".join(applied))

    def test_a_blank_value_is_skipped_not_set_to_empty(self):
        """Chain.have_key treats empty as absent; an empty string would make a
        provider look configured when it is not."""
        envd = {}
        env_mod.load(self.write("OPENROUTER_API_KEY=\n"), environ=envd)
        self.assertNotIn("OPENROUTER_API_KEY", envd)

    def test_an_existing_variable_wins(self):
        """An exported key is a deliberate override."""
        envd = {"GROQ_API_KEY": "from_the_shell"}
        applied = env_mod.load(self.write("GROQ_API_KEY=from_the_file\n"),
                               environ=envd)
        self.assertEqual(envd["GROQ_API_KEY"], "from_the_shell")
        self.assertEqual(applied, [])

    def test_override_is_available_when_asked(self):
        envd = {"GROQ_API_KEY": "old"}
        env_mod.load(self.write("GROQ_API_KEY=new\n"), environ=envd,
                     override=True)
        self.assertEqual(envd["GROQ_API_KEY"], "new")

    def test_an_empty_existing_value_is_replaced(self):
        envd = {"GROQ_API_KEY": ""}
        env_mod.load(self.write("GROQ_API_KEY=gsk_x\n"), environ=envd)
        self.assertEqual(envd["GROQ_API_KEY"], "gsk_x")

    def test_a_missing_path_is_not_an_error(self):
        """Configured but absent should degrade to 'no keys', not a traceback.
        path=None means DISCOVER, which is a different thing and will happily
        find the repo's own .env -- which is what my first version of this
        test accidentally asserted against."""
        missing = Path(self.tmp.name) / "nope" / ".env"
        self.assertEqual(env_mod.load(missing, environ={}), [])

    def test_discovery_returning_nothing_is_not_an_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as bare:
            self.assertIsNone(env_mod.find(Path(bare)))


class Discovery(unittest.TestCase):
    def test_it_finds_the_repo_env_example_structure(self):
        """find() walks upward, so it works from any module in the package."""
        found = env_mod.find(Path(__file__).resolve().parents[1] / "avi_bot")
        # .env is gitignored and may not exist on a fresh clone; either a Path
        # or None is correct, but it must never raise.
        self.assertTrue(found is None or found.name == ".env")


class ChainSeesIt(unittest.TestCase):
    def test_a_loaded_key_makes_the_provider_available(self):
        from avi_bot.brain import providers
        envd = {}
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("GROQ_API_KEY=gsk_x\nOPENROUTER_API_KEY=\n")
            env_mod.load(path, environ=envd)
        chain = providers.Chain(budget=providers.Budget(), env=envd,
                               send=lambda *a: None)
        avail = {n: ok for n, ok, why in chain.available()}
        self.assertTrue(avail["groq"])
        self.assertFalse(avail["openrouter"], "blank key looked configured")


if __name__ == "__main__":
    unittest.main()
