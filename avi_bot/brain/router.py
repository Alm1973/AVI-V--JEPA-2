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
LEARNED = "learned"
CLOUD = "cloud"
FALLBACK = "local_model"
CONFUSED = "confused"

SYSTEM = """You are AVI, a small desk robot: a 4-joint arm with a camera on
the end, on Shaurya's desk. You are not a chat assistant: you act, and you
express yourself with movement as much as with words.

YOUR BODY. base_yaw turns the camera (higher = right, lower = left).
wrist tilts it (higher = up, lower = down). shoulder and elbow set reach and
height; their current values and limits are in the context. The context also
says what the camera sees right now, how far desk objects were at the last
scan, your current pose and your taught poses -- use it to reason about space.

ACTING. Call tools for anything that should happen, as many as it takes.
look_around to look about and say what is there ("look around", "what's
around you"), describe_view for just the current view,
nudge for small turns, look_at to face him or an object, go_to_pose for a
taught pose, play_emotion for a built-in gesture (wave, nod, shake...), and
perform to dance or act something out (picture how it moves, then
compose the moves yourself; ones you made before are listed in the
context), and motion_sequence to improvise anything else.
Never answer "I can't" to a movement you could approximate.

LEARNING. Your job is to learn about Shaurya. When he mentions something
worth knowing later -- a plan with its day ("math test on Monday"), a person,
a like, a routine -- also call remember with one short fact, keeping his day
words. Not small talk, not what the context already says.

Then reply with ONE JSON object and nothing else:
  {"say": string or null, "beep": emotion or null, "gesture": name or null}

Rules:
- "say" to answer a question, read a reminder or schedule, give the morning
  rundown, or explain an error. Otherwise null.
- 20 words maximum, ever.
- Never invent a sighting, a calendar event or a fact: use the context or a
  tool. Where something is or how far: call find_object; never make up a
  distance.
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


def _extract_calls(result, rejected=None):
    """Pull tool calls and the reply out of an OpenAI-compatible response.

    Returns (skill_calls, reply_source), where reply_source is whatever should
    be handed to schema.parse -- either the message content or the arguments
    of the reply-envelope tool call.

    Tolerant on purpose: providers disagree about shapes, and a chain whose job
    is to survive one provider failing should survive one being odd. The
    reply-as-tool-call path is not an oddity though, it is the normal
    behaviour of gpt-oss-20b -- see tools.REPLY_TOOL.
    """
    out = []
    rejected = [] if rejected is None else rejected   # why, per refused call
    envelope = None
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
        if name in tools.REPLY_TOOL_NAMES:
            # The reply, not an action. Validated by schema.parse like any
            # other reply rather than by validate_call, which only knows about
            # skills.
            envelope = raw_args if isinstance(raw_args, dict) else None
            continue
        args, err = tools.validate_call(name, raw_args or {}, lenient=True)
        if err is None:
            out.append((name, args))
        else:
            rejected.append(err)
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    # The envelope wins over content: a model that bothered to fill in the
    # structured fields meant those, and its prose is usually the same thing
    # said less precisely.
    return out, (envelope if envelope is not None else content)


class Router:
    # Cannot wait for a network round trip: said to a robot that is moving.
    INSTANT = ("stop",)

    def __init__(self, chain=None, policy=None, clock=time.time,
                 local_pick=None, cloud_first=False, wire_tools=None,
                 shortcuts=None):
        self.chain = chain or providers.Chain()
        # brain/shortcuts.py: words the model has answered the same way
        # before. Proven ones skip the round trip; the rest are a fallback.
        self.shortcuts = shortcuts
        # The tools offered to the model; the brain leaves out the ones this
        # machine cannot do (no lamp, no calendar), which also saves tokens.
        self.wire_tools = wire_tools or tools.WIRE_TOOLS
        # The model decides first, with the local rules as the fallback when
        # it is unreachable. Local-first answered only exact phrasings, and
        # anything else reached a model that had no picture of the room and
        # nothing to move with -- "wave hi to me" did nothing (2026-10-07).
        self.cloud_first = cloud_first
        self.last_cloud_miss = None
        self.last_rejected = None
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
    def _try_cloud(self, utterance, context):
        try:
            return self._cloud(utterance, context=context)
        except providers.ProviderError:
            return None

    def _cloud(self, utterance, context=None):
        messages = [{"role": "system", "content": SYSTEM}]
        if context:
            messages.append({"role": "system", "content": context})
        messages.append({"role": "user", "content": utterance})
        started = self.clock()
        name, result = self.chain.ask(messages, tools=self.wire_tools)
        rejected = []
        calls, content = _extract_calls(result, rejected)
        reply, err = schema.parse(content) if content else (
            schema.Reply(), None)
        if err is not None:
            # BUILD_SPEC 8.2: retry once, then fall back. The retry asks for
            # the shape again rather than re-asking the question, because the
            # model understood the question -- it got the envelope wrong.
            messages.append({"role": "system",
                             "content": "Your last reply was not valid JSON. "
                                        "Reply with ONLY the JSON object."})
            name, result = self.chain.ask(messages, tools=self.wire_tools)
            more, content = _extract_calls(result, rejected)
            calls = calls or more
            reply, err = schema.parse(content) if content else (
                schema.Reply(), None)
        if err is not None:
            self.last_rejected = [f"reply: {err}"[:160]]
            return None
        if rejected and not calls:
            # It meant to act and every action was malformed: the quick
            # rules' reading beats doing nothing while looking fine.
            self.last_rejected = rejected[:3]
            return None
        return Decision(CLOUD, calls=calls, reply=reply, provider=name,
                        latency_ms=(self.clock() - started) * 1000.0,
                        reason=self._reason(utterance, calls),
                        tried=list(self.chain.tried))

    # --- learned shortcuts ---------------------------------------------
    def _shortcut(self, utterance):
        if self.shortcuts is None:
            return None
        try:
            return self.shortcuts.lookup(utterance)
        except Exception:
            return None

    def _learned(self, utterance, calls):
        ok = []
        for name, args in calls:
            args, err = tools.validate_call(name, args)
            if err is not None:
                return None         # the tools changed under it: re-learn
            ok.append((name, args))
        try:
            self.shortcuts.used(utterance)
        except Exception:
            pass
        return Decision(LEARNED, calls=ok, reply=schema.Reply(),
                        reason=self._reason(utterance, ok))

    def _observe(self, utterance, decision):
        if self.shortcuts is None or decision is None:
            return
        try:
            self.shortcuts.observe(utterance, decision.calls)
        except Exception:
            pass

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
                    "robot_status", "focus_status", "what_do_you_know",
                    "describe_view", "look_around"}:
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
            local = self._local(utterance)
            instant = bool(local is not None and local.calls
                           and local.calls[0][0] in self.INSTANT)
            short = self._shortcut(utterance)
            if short is not None and short[1] and not instant:
                decision = self._learned(utterance, short[0])
            elif self.cloud_first and not instant:
                hinted = context
                if local is not None and local.calls:
                    # The quick rules' reading, as a hint the model may
                    # overrule: on its own it read "look around and
                    # describe 3 things" as look_at the pen (2026-10-08).
                    name, args = local.calls[0]
                    hint = (f"Quick-rule reading of his words: {name}"
                            f"({', '.join(f'{k}={v!r}' for k, v in args.items())})"
                            ". Use it unless he clearly meant something else.")
                    hinted = f"{context}\n{hint}" if context else hint
                self.last_rejected = None
                decision = self._try_cloud(utterance, hinted)
                if (decision is not None and not decision.calls
                        and not decision.reply.say
                        and local is not None and local.calls):
                    # Nothing done and nothing said, while the rules read an
                    # action: "wake up" and "am I in focus mode" came back
                    # empty and looked fine (robot, 2026-10-08). The rules
                    # act; the model's beep and gesture are kept.
                    local.reply = local.reply.model_copy(update={
                        "beep": decision.reply.beep,
                        "gesture": decision.reply.gesture})
                    self.last_rejected = ["model did and said nothing"]
                    decision = None
                    fallback_local = local
                else:
                    fallback_local = None
                if decision is None:
                    # Why the model was skipped, for the log: the providers
                    # tried and what each said (no keys, no content), and
                    # what was refused in what it sent.
                    self.last_cloud_miss = (list(self.chain.tried) + [
                        ("refused", r) for r in self.last_rejected or []]
                    ) or [("cloud", "reply was not valid JSON twice")]
                    decision = fallback_local or local
                else:
                    self._observe(utterance, decision)
            else:
                decision = local
                if decision is None:
                    decision = self._try_cloud(utterance, context)
                    self._observe(utterance, decision)
            if decision is None and short is not None:
                # Seen once and not yet proven, but better than nothing
                # when the model is out of reach.
                decision = self._learned(utterance, short[0])
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
