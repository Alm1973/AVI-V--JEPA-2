"""The brain: routing, failover, the output contract and the speech policy.

M4's acceptance (BUILD_SPEC 15) is three things. The twenty scripted
utterances live in test_intents.py; the other two are here:
"Groq blocked -> fallback works", and "every reply follows the speech policy".

Nothing here needs a key, a network, a GPU or an arm. The network call is
injected into providers.Chain and the 3B pick is injected into Router, which
is what makes the whole decision layer testable from the Mac.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.brain import (providers, router as router_mod,   # noqa: E402
                           schema, speech, tools)


class FakeFn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeCall:
    def __init__(self, name, arguments):
        self.function = FakeFn(name, arguments)


class FakeMessage:
    def __init__(self, content=None, calls=()):
        self.content = content
        self.tool_calls = list(calls)


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeResult:
    def __init__(self, content=None, calls=()):
        self.choices = [FakeChoice(FakeMessage(content, calls))]


ALL_KEYS = {"GROQ_API_KEY": "x", "OPENROUTER_API_KEY": "x",
            "GITHUB_TOKEN": "x"}


def chain(send, env=None, today="2026-10-05"):
    return providers.Chain(budget=providers.Budget(today=lambda: today),
                           send=send, env=dict(env or ALL_KEYS))


# --- the contract ---------------------------------------------------------
class Contract(unittest.TestCase):
    def test_a_good_reply_parses(self):
        r, err = schema.parse('{"say": "Ten past four.", "beep": "neutral"}')
        self.assertIsNone(err)
        self.assertEqual(r.words, 3)

    def test_a_fenced_reply_parses(self):
        """Models fence JSON even when told not to."""
        r, err = schema.parse('```json\n{"beep": "happy"}\n```')
        self.assertIsNone(err)
        self.assertEqual(r.beep, "happy")

    def test_an_unknown_beep_is_an_error_not_a_silence(self):
        r, err = schema.parse('{"beep": "smug"}')
        self.assertIsNone(r)
        self.assertIn("smug", err)

    def test_an_extra_field_is_refused(self):
        """A model inventing a field is a model that misunderstood the task."""
        r, err = schema.parse('{"beep": "happy", "volume": 11}')
        self.assertIsNone(r)

    def test_prose_is_not_silently_repaired(self):
        r, err = schema.parse("Sure! I'll set that timer for you.")
        self.assertIsNone(r)
        self.assertIn("not JSON", err)

    def test_the_defined_failure(self):
        r = schema.confused_reply()
        self.assertEqual((r.say, r.beep, r.gesture),
                         (None, "confused", "shake"))


# --- the speech policy ----------------------------------------------------
class Policy(unittest.TestCase):
    def setUp(self):
        self.p = speech.SpeechPolicy()

    def test_an_action_gets_a_chirp_not_a_sentence(self):
        out = self.p.apply(schema.Reply(say="Turning the lamp off now."),
                           speech.ACTION)
        self.assertIsNone(out.say)
        self.assertIsNotNone(out.beep, "dropped the words AND the chirp")

    def test_an_answer_may_speak(self):
        out = self.p.apply(schema.Reply(say="Quarter past nine."),
                           speech.ANSWER)
        self.assertEqual(out.say, "Quarter past nine.")

    def test_twenty_words_hard(self):
        long = " ".join(f"word{i}" for i in range(40))
        out = self.p.apply(schema.Reply(say=long), speech.ANSWER)
        self.assertLessEqual(out.words, speech.MAX_WORDS)

    def test_truncation_prefers_a_sentence_boundary(self):
        text = ("Your phone was on the desk at four. " * 3).strip()
        out = self.p.apply(schema.Reply(say=text), speech.ANSWER)
        self.assertTrue(out.say.endswith("."), out.say)
        self.assertLessEqual(out.words, speech.MAX_WORDS)

    def test_an_error_is_explained_once(self):
        first = self.p.apply(schema.Reply(say="The lamp did not answer."),
                             speech.ERROR, error_key="lamp-timeout")
        second = self.p.apply(schema.Reply(say="The lamp did not answer."),
                              speech.ERROR, error_key="lamp-timeout")
        self.assertIsNotNone(first.say)
        self.assertIsNone(second.say, "narrated the same failure twice")

    def test_quiet_mode_silences_everything(self):
        self.p.set_quiet(True)
        out = self.p.apply(schema.Reply(say="Quarter past nine."),
                           speech.ANSWER)
        self.assertIsNone(out.say)

    def test_sentences_split_for_streaming(self):
        out = schema.Reply(say="Found it. On the desk.")
        self.assertEqual(self.p.sentences(out), ["Found it.", "On the desk."])


# --- the provider chain ---------------------------------------------------
class Failover(unittest.TestCase):
    def test_groq_blocked_falls_through_to_openrouter(self):
        """M4's acceptance: Groq blocked -> the fallback works."""
        seen = []

        def send(name, url, model, messages, tool_list):
            seen.append(name)
            if name == "groq":
                raise providers.ProviderError("groq", "429 rate limited")
            return FakeResult(content='{"beep": "happy"}')

        c = chain(send)
        name, result = c.ask([{"role": "user", "content": "hi"}])
        self.assertEqual(name, "openrouter")
        self.assertEqual(seen, ["groq", "openrouter"])

    def test_a_missing_key_is_skipped_without_a_request(self):
        seen = []

        def send(name, *a):
            seen.append(name)
            return FakeResult(content='{"beep": "happy"}')

        c = chain(send, env={"OPENROUTER_API_KEY": "x"})
        name, _ = c.ask([{"role": "user", "content": "hi"}])
        self.assertEqual(name, "openrouter")
        self.assertNotIn("groq", seen, "sent a request with no key")

    def test_the_budget_guard_skips_before_sending(self):
        """Discovering a limit by being refused is fine free and catastrophic
        paid, so the count is checked BEFORE the request."""
        seen = []

        def send(name, *a):
            seen.append(name)
            return FakeResult(content='{"beep": "happy"}')

        c = chain(send)
        # OpenRouter's free tier is 50/day; 80% of that is 40.
        for _ in range(40):
            c.budget.note("openrouter")
        self.assertFalse(c.budget.allows("openrouter", 50))
        avail = dict((n, ok) for n, ok, why in c.available())
        self.assertFalse(avail["openrouter"])
        self.assertTrue(avail["groq"])

    def test_everything_failing_raises_rather_than_hanging(self):
        def send(name, *a):
            raise providers.ProviderError(name, "down")

        c = chain(send)
        with self.assertRaises(providers.ProviderError):
            c.ask([{"role": "user", "content": "hi"}])

    def test_failures_are_counted_separately(self):
        def send(name, *a):
            if name == "groq":
                raise providers.ProviderError("groq", "500")
            return FakeResult(content='{"beep": "happy"}')

        c = chain(send)
        c.ask([{"role": "user", "content": "hi"}])
        report = c.budget.report()
        self.assertEqual(report["groq"]["failures"], 1)
        self.assertEqual(report["openrouter"]["failures"], 0)


class Redaction(unittest.TestCase):
    def test_keys_never_leave(self):
        for secret in ("sk_live_abcdefghijklmnop", "gsk_ABCDEFGHIJKLMNOP",
                       "xpl_0123456789abcdef0123456789abcdef01234567",
                       "ghp_abcdefghijklmnopqrst"):
            self.assertNotIn(secret, providers.redact(f"use {secret} now"))

    def test_the_calendar_url_never_leaves(self):
        """The secret iCal address IS the credential: a provider that logs it
        has read access to his calendar indefinitely."""
        url = "https://calendar.google.com/calendar/ical/abc123/basic.ics"
        self.assertNotIn("abc123", providers.redact(f"schedule at {url}"))

    def test_redaction_reaches_the_messages(self):
        sent = {}

        def send(name, url, model, messages, tool_list):
            sent["messages"] = messages
            return FakeResult(content='{"beep": "happy"}')

        c = chain(send)
        c.ask([{"role": "user", "content": "my key is gsk_ABCDEFGHIJKLMNOP"}])
        self.assertNotIn("gsk_ABCDEFGHIJKLMNOP",
                         sent["messages"][0]["content"])


# --- routing --------------------------------------------------------------
def make_router(send=None, local_pick=None, env=None):
    def default(name, url, model, messages, tool_list):
        raise providers.ProviderError(name, "no network in tests")
    return router_mod.Router(chain=chain(send or default, env=env),
                             local_pick=local_pick)


class Routing(unittest.TestCase):
    def test_a_frequent_command_never_touches_the_cloud(self):
        def send(*a):
            raise AssertionError("the cloud was called for 'lamp off'")

        d = make_router(send=send).handle("turn the lamp off")
        self.assertEqual(d.route, router_mod.LOCAL)
        self.assertEqual(d.calls, [("lamp", {"action": "off"})])

    def test_an_open_question_goes_to_the_cloud(self):
        def send(name, url, model, messages, tool_list):
            return FakeResult(content='{"say": "Because the belt slips.",'
                                      ' "beep": "curious"}')

        d = make_router(send=send).handle("why does the elbow buzz when you "
                                          "lean forward")
        self.assertEqual(d.route, router_mod.CLOUD)
        self.assertEqual(d.reply.say, "Because the belt slips.")

    def test_tool_calls_come_back_validated(self):
        def send(name, url, model, messages, tool_list):
            return FakeResult(content='{"beep": "neutral"}',
                              calls=[FakeCall("set_timer",
                                              '{"seconds": 300}')])

        d = make_router(send=send).handle("put something on for five minutes "
                                          "while I make tea")
        self.assertEqual(d.calls, [("set_timer", {"seconds": 300})])

    def test_an_invalid_tool_call_is_dropped_not_guessed(self):
        def send(name, url, model, messages, tool_list):
            return FakeResult(content='{"beep": "neutral"}',
                              calls=[FakeCall("set_timer",
                                              '{"seconds": "ages"}')])

        d = make_router(send=send).handle("set something going for ages")
        self.assertEqual(d.calls, [])

    def test_the_cloud_failing_reaches_the_local_model(self):
        """M4: the chain is exhausted, and the 3B pick still answers."""
        picked = []

        def pick(utterance, choices):
            picked.append(choices)
            return "get_time"

        d = make_router(local_pick=pick).handle("so what's the hour then")
        self.assertEqual(d.route, router_mod.FALLBACK)
        self.assertEqual(d.calls, [("get_time", {})])
        self.assertIn("get_time", picked[0])

    def test_nothing_working_gives_a_confused_chirp(self):
        d = make_router().handle("ruminate on the nature of belt tension")
        self.assertEqual(d.route, router_mod.CONFUSED)
        self.assertEqual(d.reply.beep, "confused")
        self.assertEqual(d.reply.gesture, "shake")

    def test_handle_never_raises(self):
        def send(*a):
            raise RuntimeError("provider exploded in an unexpected way")

        for text in ("", None, "x" * 5000, "turn the lamp off", "???"):
            d = make_router(send=send).handle(text)
            self.assertIsInstance(d, router_mod.Decision)
            self.assertIsNotNone(d.reply)

    def test_bad_json_is_retried_once_then_abandoned(self):
        calls = []

        def send(name, url, model, messages, tool_list):
            calls.append(len(messages))
            return FakeResult(content="Sure thing, boss!")

        d = make_router(send=send).handle("what do you make of all this")
        self.assertGreaterEqual(len(calls), 2, "did not retry the envelope")
        self.assertEqual(d.route, router_mod.CONFUSED)

    def test_the_policy_applies_to_every_route(self):
        """A sentence must not sneak through on a path that forgot to check."""
        def send(name, url, model, messages, tool_list):
            return FakeResult(content='{"say": "Done, lamp is off now boss."}',
                              calls=[FakeCall("lamp", '{"action": "off"}')])

        d = make_router(send=send).handle("kill the lights would you")
        self.assertEqual(d.route, router_mod.CLOUD)
        self.assertIsNone(d.reply.say, "spoke for a plain action")


class ToolSchemas(unittest.TestCase):
    def test_every_local_intent_names_a_real_tool(self):
        """The two routes must emit the same names, or a skill works when
        phrased one way and fails when phrased another."""
        from avi_bot.brain import intents
        seen = set()
        for text in [t for t, _, _ in __import__(
                "tests.test_intents", fromlist=["SCRIPTED"]).SCRIPTED]:
            i = intents.match(text)
            if i:
                seen.add(i.tool)
        missing = seen - set(tools.NAMES)
        self.assertEqual(missing, set(), f"intents emit unknown tools: {missing}")

    def test_local_choices_are_all_real_tools(self):
        self.assertEqual(set(tools.LOCAL_CHOICES) - set(tools.NAMES), set())

    def test_enum_values_are_enforced(self):
        args, err = tools.validate_call("lamp", {"action": "dim"})
        self.assertIsNone(args)
        self.assertIn("dim", err)

    def test_required_args_are_enforced(self):
        args, err = tools.validate_call("find_object", {})
        self.assertIsNone(args)
        self.assertIn("name", err)

    def test_an_unlisted_tool_is_refused(self):
        args, err = tools.validate_call("rm_rf", {})
        self.assertIsNone(args)


if __name__ == "__main__":
    unittest.main()


class TheModelDecidesFirst(unittest.TestCase):
    """Local-first answered only exact phrasings; everything else reached a
    model that could neither see nor move."""

    def router(self, send):
        return router_mod.Router(chain=chain(send), cloud_first=True)

    def test_even_a_known_phrase_goes_to_the_model(self):
        seen = []

        def send(name, url, model, messages, tool_list):
            seen.append(messages[-1]["content"])
            return FakeResult(content='{"beep": "neutral"}',
                              calls=[FakeCall("lamp", '{"action": "off"}')])
        d = self.router(send).handle("turn the lamp off")
        self.assertEqual(seen, ["turn the lamp off"])
        self.assertEqual(d.route, router_mod.CLOUD)

    def test_stop_never_waits_for_the_network(self):
        def send(*a):
            raise AssertionError("'stop' went to the cloud")
        d = self.router(send).handle("stop")
        self.assertEqual(d.calls, [("stop", {})])

    def test_the_local_rules_catch_it_when_the_cloud_is_down(self):
        def send(name, *a):
            raise providers.ProviderError(name, "offline")
        d = self.router(send).handle("turn the lamp off")
        self.assertEqual(d.route, router_mod.LOCAL)

    def test_it_is_told_it_can_improvise_movement(self):
        self.assertIn("motion_sequence", router_mod.SYSTEM)
        names = {t["function"]["name"] for t in tools.WIRE_TOOLS}
        self.assertTrue({"nudge", "move_joints", "motion_sequence",
                         "go_to_pose"} <= names)
