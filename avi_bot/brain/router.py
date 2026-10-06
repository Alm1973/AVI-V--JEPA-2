"""Three routes, tried in order. BUILD_SPEC 8.1.

    1. local rules      instant, offline, only the frequent commands
    2. cloud LLM        everything open-ended, with tools
    3. local fallback    a 3B model picking from a short intent list
       ...and if that fails too, a confused chirp and a shake.

The ordering is a latency and a reliability decision at once. "Lamp off"
should never wait on a network round trip, and it should still work when every
free tier is exhausted -- so the commands that happen twenty times a day do
not touch a model at all, and the model handles the things only a model can.

WHAT THIS MODULE DOES NOT DO
It does not execute tools. It decides WHAT should happen and returns it, and
something with a serial port and a lamp credential carries it out. Keeping the
decision pure is what lets the whole router be tested with no keys, no
network, no GPU and no arm -- which is the only reason any of this could be
written from the Mac.
"""
import json
import time

from . import intents, providers, schema, speech, tools

LOCAL = "local_rules"
CLOUD = "cloud"
FALLBACK = "local_model"
CONFUSED = "confused"

SYSTEM = """You are AVI, a small desk robot with a 4-joint arm and a camera.
You are not a chat assistant: you act, and you mostly express yourself with
beeps and movement rather than words.

Call a tool for anything that should happen. Then reply with ONE JSON object
and nothing else:
  {"say": string or null, "beep": emotion or null, "gesture": name or null}

Rules:
- "say" only to answer a question, read a reminder or schedule, give the
  morning rundown, or explain an error. Otherwise null.
- 20 words maximum, ever.
- Prefer a beep to a sentence.
- Never invent a sighting, a calendar event or a fact. Call the tool.
"""


class Decision:
    __slots__ = ("route", "calls", "reply", "provider", "latency_ms",
                 "reason", "error", "tried")

    def __init__(self, route, calls=None, reply=None, provider=None,
                 latency_ms=0.0, reason=speech.CHAT, error=None, tried=None):
        self.route = route
        self.calls = calls or []
        self.reply = reply
        self.provider = provider
        self.latency_ms = latency_ms
        self.reason = reason
        self.error = error
        self.tried = tried or []

    def __repr__(self):
        names = ",".join(c[0] for c in self.calls) or "-"
        return (f"<Decision {self.route} tools={names} "
                f"say={self.reply.say!r} beep={self.reply.beep}>")


def _extract_calls(result):
    """Pull tool calls out of an OpenAI-compatible response. Tolerant on
    purpose: providers disagree about shapes, and a chain whose job is to
    survive one provider failing should survive one provider being odd."""
    out = []
    choice = None
    try:
        choice = result.choices[0]
    except Exception:
        if isinstance(result, dict):
            choice = (result.get("choices") or [None])[0]
    if choice is None:
        return out, None
    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message")
    if message is None:
        return out, None
    raw_calls = getattr(message, "tool_calls", None)
    if raw_calls is None and isinstance(message, dict):
        raw_calls = message.get("tool_calls")
    for call in raw_calls or []:
        fn = getattr(call, "function", None) or (
            call.get("function") if isinstance(call, dict) else None)
        if fn is None:
            continue
        name = getattr(fn, "name", None) or (
            fn.get("name") if isinstance(fn, dict) else None)
        raw_args = getattr(fn, "arguments", None) or (
            fn.get("arguments") if isinstance(fn, dict) else None)
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args or "{}")
            except Exception:
                raw_args = None
        args, err = tools.validate_call(name, raw_args or {})
        if err is None:
            out.append((name, args))
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return out, content


class Router:
    def __init__(self, chain=None, policy=None, clock=time.time,
                 local_pick=None):
        self.chain = chain or providers.Chain()
        self.policy = policy or speech.SpeechPolicy()
        self.clock = clock
        # Injected so the 3B multiple-choice fallback can be tested without a
        # GPU. Takes (utterance, choices) and returns a name or None.
        self.local_pick = local_pick
        self.log = []

    # --- route 1 --------------------------------------------------------
    def _local(self, utterance):
        intent = intents.match(utterance)
        if intent is None:
            return None
        args, err = tools.validate_call(intent.tool, intent.args)
        if err is not None:
            # A local rule that produces an invalid call is a bug in the rule,
            # not a reason to act. Fall through and let the model read it.
            return None
        reply = schema.Reply(beep=None, gesture=None, say=None)
        return Decision(LOCAL, calls=[(intent.tool, args)], reply=reply,
                        reason=intent.reason)

    # --- route 2 --------------------------------------------------------
    def _cloud(self, utterance, context=None):
        messages = [{"role": "system", "content": SYSTEM}]
        if context:
            messages.append({"role": "system", "content": context})
        messages.append({"role": "user", "content": utterance})
        started = self.clock()
        name, result = self.chain.ask(messages, tools=tools.TOOLS)
        calls, content = _extract_calls(result)
        reply, err = schema.parse(content) if content else (
            schema.Reply(), None)
        if err is not None:
            # BUILD_SPEC 8.2: retry once, then fall back. The retry asks for
            # the shape again rather than re-asking the question, because the
            # model understood the question -- it got the envelope wrong.
            messages.append({"role": "system",
                             "content": "Your last reply was not valid JSON. "
                                        "Reply with ONLY the JSON object."})
            name, result = self.chain.ask(messages, tools=tools.TOOLS)
            more, content = _extract_calls(result)
            calls = calls or more
            reply, err = schema.parse(content) if content else (
                schema.Reply(), None)
        if err is not None:
            return None
        return Decision(CLOUD, calls=calls, reply=reply, provider=name,
                        latency_ms=(self.clock() - started) * 1000.0,
                        reason=self._reason(utterance, calls),
                        tried=list(self.chain.tried))

    # --- route 3 --------------------------------------------------------
    def _fallback(self, utterance):
        if self.local_pick is None:
            return None
        name = self.local_pick(utterance, tools.LOCAL_CHOICES)
        if not name or name not in tools.BY_NAME:
            return None
        args, err = tools.validate_call(name, {})
        if err is not None:
            # Needs arguments the multiple-choice path cannot supply.
            return None
        return Decision(FALLBACK, calls=[(name, args)], reply=schema.Reply(),
                        reason=speech.ACTION)

    def _reason(self, utterance, calls):
        """Why speaking might be allowed. The POLICY still decides."""
        names = {c[0] for c in calls}
        if names & {"get_schedule", "set_reminder"}:
            return speech.REMINDER
        if names & {"list_timers", "find_object", "get_time", "weather",
                    "robot_status", "focus_status", "what_do_you_know"}:
            return speech.ANSWER
        if speech.is_question(utterance):
            return speech.ANSWER
        if names:
            return speech.ACTION
        return speech.CHAT

    # --- the one entry point -------------------------------------------
    def handle(self, utterance, context=None):
        """Decide what should happen. Never raises, always returns a Decision.

        The speech policy is applied here, at the end, once, to every route --
        so a sentence cannot sneak through just because it came back from a
        path that forgot to check.
        """
        started = self.clock()
        decision = None
        try:
            decision = self._local(utterance)
            if decision is None:
                try:
                    decision = self._cloud(utterance, context=context)
                except providers.ProviderError:
                    decision = None
                if decision is None:
                    decision = self._fallback(utterance)
        except Exception as exc:                 # never take the robot down
            decision = Decision(CONFUSED, reply=schema.confused_reply(),
                                reason=speech.ERROR, error=repr(exc))
        if decision is None:
            decision = Decision(CONFUSED, reply=schema.confused_reply(),
                                reason=speech.ERROR,
                                error="no route produced a decision")
        decision.reply = self.policy.apply(
            decision.reply, decision.reason,
            error_key=decision.error if decision.reason == speech.ERROR
            else None)
        if not decision.latency_ms:
            decision.latency_ms = (self.clock() - started) * 1000.0
        self.log.append((utterance, decision.route, decision.provider,
                         round(decision.latency_ms, 1)))
        return decision
