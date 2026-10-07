"""The cloud route works without the openai SDK (the MSI does not have it)."""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.brain import providers, router                       # noqa: E402

KEY = "gsk_test_not_a_real_key"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class PlainHttp(unittest.TestCase):
    def chain(self, opener):
        c = providers.Chain(env={"GROQ_API_KEY": KEY})
        c.urlopen = opener
        return c

    def test_posts_openai_shaped_json_and_returns_a_dict(self):
        seen = {}

        def opener(req, timeout):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            seen["body"] = json.loads(req.data)
            return Resp(json.dumps({"choices": [{"message": {
                "content": None, "tool_calls": [{"function": {
                    "name": "list_timers", "arguments": "{}"}}]}}]}).encode())

        c = self.chain(opener)
        got = c._http_send("groq", "https://api.groq.com/openai/v1",
                           "m", [{"role": "user", "content": "hi"}], [])
        self.assertEqual(seen["url"],
                         "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer " + KEY)
        self.assertEqual(seen["body"]["model"], "m")
        self.assertIsInstance(got, dict)

    def test_the_router_reads_the_dict(self):
        result = {"choices": [{"message": {"content": None, "tool_calls": [
            {"function": {"name": "list_timers", "arguments": "{}"}}]}}]}
        got = router._extract_calls(result)
        calls = got[0] if isinstance(got, tuple) else got
        self.assertIn("list_timers", [c[0] for c in calls])

    def test_an_http_error_never_carries_the_key(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                         {}, io.BytesIO(b'{"error":"bad"}'))
        c = self.chain(opener)
        with self.assertRaises(providers.ProviderError) as cm:
            c._http_send("groq", "https://x/v1", "m", [], None)
        self.assertIn("401", str(cm.exception.why))
        self.assertNotIn(KEY, str(cm.exception.why))


if __name__ == "__main__":
    unittest.main()
