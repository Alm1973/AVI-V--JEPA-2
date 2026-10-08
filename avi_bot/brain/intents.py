"""Local rules, instant and offline. BUILD_SPEC 8.1 step 1.

The frequent commands must work with the network unplugged, the GPU busy and
every provider rate-limited. "Lamp off" waiting on a cloud round trip is a
worse product than no lamp control at all, and a timer that fails because a
free tier ran out is not a timer.

So this layer is pure regex over the commands BUILD_SPEC lists as frequent,
and it is allowed to return NOTHING. A rule that fires on a sentence it half
recognises is far worse than falling through to a model that will read the
whole thing -- a wrong local match is instant, confident and silent about its
doubt. Each pattern is therefore anchored and specific, and the ambiguous
middle ground is left to the cloud on purpose.

Returns an Intent (tool name plus arguments), or None.
"""
import re

from .. import numbers

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50,
    "sixty": 60, "ninety": 90, "half": 30,
}
# NOTE "a" and "an" are deliberately absent. They read as durations only in
# "a minute" / "an hour", which the unit patterns below already handle, and
# including them made "set A timer for 5 minutes" parse "a" as the duration
# and "5 minutes" as the LABEL -- a one-minute timer, confidently, instantly.


class Intent:
    __slots__ = ("tool", "args", "pattern", "reason")

    def __init__(self, tool, args=None, pattern=None, reason="action"):
        self.tool = tool
        self.args = args or {}
        self.pattern = pattern
        self.reason = reason

    def __eq__(self, o):
        return (isinstance(o, Intent) and o.tool == self.tool
                and o.args == self.args)

    def __repr__(self):
        return f"<Intent {self.tool} {self.args}>"


def _minutes(text):
    """'20', 'twenty', 'half an hour', '1 hour' -> minutes, or None."""
    if text is None:
        return None
    t = text.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(h|hr|hrs|hour|hours)", t)
    if m:
        return int(m.group(1)) * 60
    m = re.fullmatch(r"(\d+)\s*(m|min|mins|minute|minutes)?", t)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d+)\s*(s|sec|secs|second|seconds)", t)
    if m:
        # Sub-minute timers are real ("5 second timer") and must not round to 0.
        return max(1, round(int(m.group(1)) / 60))
    if t.startswith("half"):
        return 30
    # Spoken numbers. Transcripts write "twenty five minutes", never "25", so
    # a parser that only reads digits works typed and fails spoken -- the
    # harder bug to notice, since the typed path is the one tried by hand.
    m = re.fullmatch(r"(?P<n>[a-z][a-z\s-]*?)\s*"
                     r"(?P<unit>m|mins?|minutes?|h|hrs?|hours?)?", t)
    if m:
        n = numbers.words_to_int(m.group("n"), allow_quantifiers=True)
        if n:
            unit = (m.group("unit") or "m").lower()
            return n * 60 if unit.startswith("h") else n
    return None


def _seconds(text):
    if text is None:
        return None
    t = text.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(s|sec|secs|second|seconds)", t)
    if m:
        return int(m.group(1))
    mins = _minutes(t)
    return mins * 60 if mins is not None else None


# (compiled pattern, builder). First match wins, so order is significance
# order: the more specific phrasing has to come first.
RULES = []


def rule(pattern):
    def wrap(fn):
        RULES.append((re.compile(pattern, re.I), fn))
        return fn
    return wrap


# --- timers ---------------------------------------------------------------
# "timer for X" is registered FIRST, before the looser "X timer" form: that
# one can match almost anything followed by the word timer, so it has to be
# the last resort rather than the first guess.
@rule(r"^(?:set\s+)?(?:a\s+|an\s+)?timer\s+(?:for\s+)?"
      r"(?P<dur>[\w\s]+?)(?:\s+(?:called|named)\s+(?P<label>.+))?$")
def _timer(m):
    secs = _seconds(m.group("dur"))
    if not secs:
        return None
    args = {"seconds": secs}
    label = (m.groupdict().get("label") or "").strip()
    if label:
        args["label"] = label
    return Intent("set_timer", args)


@rule(r"^(?:set\s+)?(?:a\s+|an\s+)?(?P<dur>[\w\s]+?)\s+timer"
      r"(?:\s+(?:for|called|named)\s+(?P<label>.+))?$")
def _timer_suffix(m):
    return _timer(m)


@rule(r"^(?:cancel|stop|kill)\s+(?:the\s+)?timer(?:s)?$")
def _cancel_timer(m):
    return Intent("cancel_timer", {})


@rule(r"^(?:what\s+)?timers?(?:\s+are\s+(?:there|left|running))?\??$")
@rule(r"^list\s+(?:my\s+)?timers$")
def _list_timers(m):
    return Intent("list_timers", {}, reason="answer")


# --- focus ----------------------------------------------------------------
@rule(r"^(?:let'?s\s+)?focus(?:\s+(?:for|mode))?\s+(?P<dur>[\w\s]+)$")
@rule(r"^(?:start\s+)?focus(?:\s+mode)?$")
def _focus(m):
    mins = _minutes(m.groupdict().get("dur")) if m.groupdict().get("dur") else None
    return Intent("focus_start", {"minutes": mins} if mins else {})


@rule(r"^(?:stop|end|cancel)\s+focus(?:\s+mode)?$")
def _focus_stop(m):
    return Intent("focus_stop", {})


@rule(r"^(?:how(?:'s| is)\s+)?focus(?:\s+status)?\??$")
def _focus_status(m):
    return Intent("focus_status", {}, reason="answer")


# --- lamp -----------------------------------------------------------------
@rule(r"^(?:turn\s+)?(?:the\s+)?(?:lamp|light|lights)\s+(?P<state>on|off)$")
@rule(r"^(?:turn\s+)?(?P<state>on|off)\s+(?:the\s+)?(?:lamp|light|lights)$")
def _lamp(m):
    return Intent("lamp", {"action": m.group("state").lower()})


@rule(r"^(?:toggle\s+)?(?:the\s+)?(?:lamp|light|lights)(?:\s+toggle)?$")
def _lamp_toggle(m):
    return Intent("lamp", {"action": "toggle"})


# --- media ----------------------------------------------------------------
# Bare "stop" is NOT here. BUILD_SPEC 8.1 lists stop among the modes, and a
# bare "stop" has to halt the robot rather than pause music: the two have very
# different costs when guessed wrong, and only one of them is why someone
# raises their voice at a moving arm.
@rule(r"^(?P<a>play|pause|resume)\s*(?:the\s+)?(?:music|song|audio|media)?$")
def _media(m):
    return Intent("media", {"action": "play_pause"})


@rule(r"^stop\s+(?:the\s+)?(?:music|song|audio|media|playback)$")
def _media_stop(m):
    return Intent("media", {"action": "stop"})


@rule(r"^(?:next|skip)(?:\s+(?:song|track))?$")
def _next(m):
    return Intent("media", {"action": "next"})


@rule(r"^(?:previous|prev|back|last)(?:\s+(?:song|track))?$")
def _prev(m):
    return Intent("media", {"action": "previous"})


@rule(r"^(?:turn\s+(?:it\s+)?|volume\s+)?(?P<d>up|down)(?:\s+the\s+volume)?$")
@rule(r"^volume\s+(?P<d>up|down)$")
def _volume(m):
    return Intent("media", {"action": "volume_" + m.group("d").lower()})


@rule(r"^(?:mute|unmute)$")
def _mute(m):
    return Intent("media", {"action": "mute"})


# --- memory ---------------------------------------------------------------
# Local, not left to the model: asked "what do you know about me", the cloud
# model answered "I have no stored facts" WITHOUT calling the tool -- with a
# fact stored and an afternoon of observations behind it. A question about
# what AVI knows must never be answered from what the model guesses.
_ABOUT_HIM = {"me", "myself", "shaurya", "him", "my stuff", "my things"}


def _about(text):
    q = (text or "").strip().rstrip("?").strip()
    q = re.sub(r"^(?:my|the)\s+", "", q, flags=re.I)
    return None if not q or q.lower() in _ABOUT_HIM else q


@rule(r"^what\s+(?:do\s+you\s+know|have\s+you\s+(?:noticed|learned|"
      r"learnt|seen))(?:\s+about\s+(?P<q>.+?))?\??$")
@rule(r"^what\s+do\s+you\s+remember(?:\s+about\s+(?P<q>.+?))?\??$")
def _what_do_you_know(m):
    q = _about(m.groupdict().get("q"))
    return Intent("what_do_you_know", {"query": q} if q else {},
                  reason="answer")


@rule(r"^remember\s+(?!to\s)(?:that\s+)?(?P<fact>.{3,280})$")
def _remember(m):
    return Intent("remember", {"text": m.group("fact").strip()})


@rule(r"^forget\s+(?:that\s+|about\s+)?(?P<fact>.{2,280})$")
def _forget(m):
    return Intent("forget", {"text": m.group("fact").strip()})


# Basic moves, for when the model is out of reach (rate-limited, offline):
# the model decides first, and these keep the arm answering without it.
@rule(r"^(?:can\s+(?:you|u)\s+)?(?:look|turn|move)\s+"
      r"(?P<small>a\s+(?:little\s+)?bit\s+|a\s+little\s+|slightly\s+)?"
      r"(?:to\s+(?:the|your)\s+)?(?P<dir>left|right|up|down)"
      r"(?P<small2>\s+a\s+(?:little\s+)?bit|\s+a\s+little|\s+slightly)?"
      r"[.!?]?$")
def _nudge(m):
    small = m.group("small") or m.group("small2")
    return Intent("nudge", {"direction": m.group("dir"),
                            "degrees": 8 if small else 15})


@rule(r"^(?:can\s+(?:you|u)\s+)?look\s+around(?:\s+.*)?[.!?]?$")
def _look_around(m):
    n = re.search(r"\b([1-6])\b", m.group(0))
    return Intent("look_around", {"count": int(n.group(1))} if n else {},
                  reason="answer")


@rule(r"^what\s+(?:do\s+you|can\s+you|are\s+you\s+looking\s+at|"
      r"is\s+in\s+front\s+of\s+you)(?:\s+see)?(?:\s+(?:right\s+)?now)?\??$")
def _describe_view(m):
    return Intent("describe_view", {}, reason="answer")


@rule(r"^(?:can\s+(?:you|u)\s+)?(?:wave|say\s+hi)(?:\s+(?:hi|hello))?"
      r"(?:\s+to\s+me)?[.!?]?$")
def _wave(m):
    return Intent("play_emotion", {"name": "wave"})


@rule(r"^(?:can\s+(?:you|u)\s+)?(?P<g>nod|shake\s+your\s+head)[.!?]?$")
def _nod_shake(m):
    return Intent("play_emotion",
                  {"name": "nod" if m.group("g") == "nod" else "shake"})


@rule(r"^(?:look|turn)\s+(?:at|to|towards?)\s+(?:me|here|over\s+here)"
      r"[.!]?$")
@rule(r"^(?:hey\s+)?(?:avi,?\s+)?look\s+at\s+me(?:\s+avi)?[.!]?$")
def _look_at_me(m):
    # Went to the cloud, which could answer without calling the tool.
    return Intent("look_at", {"target": "me"})


@rule(r"^what\s+timers?\s+(?:do\s+i\s+have|are\s+(?:on|set))\??$")
def _which_timers(m):
    return Intent("list_timers", {}, reason="answer")


# --- finding things -------------------------------------------------------
@rule(r"^(?:where(?:'s| is| are)\s+)(?:my\s+|the\s+)?(?P<what>.+?)\??$")
@rule(r"^(?:have\s+you\s+seen\s+)(?:my\s+|the\s+)?(?P<what>.+?)\??$")
@rule(r"^find\s+(?:my\s+|the\s+)?(?P<what>.+)$")
def _find(m):
    what = m.group("what").strip().rstrip("?")
    if not what or len(what) > 40:
        return None
    return Intent("find_object", {"name": what}, reason="answer")


# --- time -----------------------------------------------------------------
@rule(r"^(?:what(?:'s| is)\s+)?(?:the\s+)?time(?:\s+is\s+it)?\??$")
@rule(r"^what\s+time\s+is\s+it\??$")
def _time(m):
    return Intent("get_time", {}, reason="answer")


# --- feedback -------------------------------------------------------------
@rule(r"^(?:good|nice|well done|thank you|thanks|great)"
      r"(?:\s+(?:job|boy|robot|avi|work))?!?$")
def _praise(m):
    return Intent("praise", {})


@rule(r"^(?:bad|no|stop it|wrong|don'?t)"
      r"(?:\s+(?:robot|avi|that))?!?$")
def _scold(m):
    return Intent("scold", {})


# --- modes ----------------------------------------------------------------
@rule(r"^(?:go\s+to\s+)?sleep$")
def _sleep(m):
    return Intent("sleep", {})


@rule(r"^(?:wake(?:\s+up)?|good\s+morning)$")
def _wake(m):
    return Intent("wake", {})


@rule(r"^(?:be\s+)?quiet(?:\s+(?:for\s+)?(?P<dur>[\w\s]+))?$")
def _quiet(m):
    mins = _minutes(m.groupdict().get("dur")) if m.groupdict().get("dur") else None
    return Intent("quiet", {"minutes": mins} if mins else {})


@rule(r"^guest\s+mode(?:\s+(?:on|off))?$")
def _guest(m):
    return Intent("guest_mode", {})


@rule(r"^(?:stop|halt|freeze|cancel)!?$")
def _stop(m):
    return Intent("stop", {})


# --- schedule -------------------------------------------------------------
@rule(r"^(?:what(?:'s| is)\s+)?(?:on\s+)?(?:my\s+)?(?:schedule|calendar|agenda)"
      r"(?:\s+(?:for\s+)?(?P<day>today|tomorrow|this week))?\??$")
def _schedule(m):
    day = (m.groupdict().get("day") or "today").lower()
    return Intent("get_schedule", {"day": day}, reason="reminder")


def match(utterance):
    """First matching rule, or None. Normalises whitespace and punctuation."""
    if not utterance:
        return None
    text = " ".join(utterance.split()).strip()
    text = re.sub(r"^(?:hey\s+)?(?:avi|jarvis)[,\s]+", "", text, flags=re.I)
    text = text.strip().rstrip(".!")
    if not text:
        return None
    for pattern, fn in RULES:
        m = pattern.match(text)
        if not m:
            continue
        intent = fn(m)
        if intent is not None:
            intent.pattern = pattern.pattern
            return intent
    return None
