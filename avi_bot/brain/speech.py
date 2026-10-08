"""When AVI is allowed to use words, and how many. BUILD_SPEC 7.2.

The default is silence. A desk robot that comments on everything is
exhausting within a day, and the whole character depends on words being rare
enough to mean something -- so this is a WHITELIST, not a filter:

    answer a question with information
    read a reminder or a schedule
    give the morning rundown
    explain an error, once

Everything else gets a chirp. The brain proposes `say` and `beep` separately
(see schema.py) precisely so that prose arriving for "lamp off" can be dropped
rather than obeyed.

Twenty words, hard. Longer replies get truncated at a sentence boundary rather
than mid-clause, because a sentence cut in half sounds like a crash and a
shorter complete sentence just sounds terse.
"""
import re

from . import schema

# The only reasons to speak.
ANSWER = "answer"            # a question that needs information back
REMINDER = "reminder"        # a due reminder or a schedule read-out
RUNDOWN = "rundown"          # the morning three-item rundown
ERROR = "error"              # explaining a failure, once
ACTION = "action"            # a command was carried out: chirp, no words
CHAT = "chat"                # anything else: chirp, no words

MAY_SPEAK = (ANSWER, REMINDER, RUNDOWN, ERROR)

MAX_WORDS = schema.MAX_WORDS

# An error is explained ONCE. The same failure repeating is the same failure,
# and narrating it every time is how a robot becomes a nag.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def is_question(utterance):
    """Good enough, and deliberately not a model call.

    Deciding whether to speak must not itself depend on the cloud, or a
    provider outage turns into a robot that refuses to answer anything.
    """
    if not utterance:
        return False
    u = utterance.strip().lower()
    if u.endswith("?"):
        return True
    openers = ("what", "when", "where", "who", "why", "how", "which",
               "is ", "are ", "do ", "does ", "did ", "can ", "could ",
               "will ", "would ", "should ", "am ", "was ", "were ",
               "have ", "has ", "tell me", "how many", "how much")
    if u.startswith(openers):
        return True
    # Asked for information without a question's shape: "look around and
    # describe 3 things you see" was answered with silence.
    asks = ("describe", "tell me", "explain", "list ", "name the",
            "how many", "what you see", "what's there", "whats there")
    return any(a in u for a in asks)


def truncate(text, max_words=MAX_WORDS):
    """At most `max_words`, cut at a sentence end where possible."""
    if not text:
        return None
    words = text.split()
    if len(words) <= max_words:
        return text
    # Prefer the longest run of whole sentences that fits.
    kept, used = [], 0
    for sentence in _SENTENCE.split(text):
        n = len(sentence.split())
        if used + n > max_words:
            break
        kept.append(sentence)
        used += n
    if kept:
        return " ".join(kept).strip()
    # One very long sentence: cut it and mark the cut honestly.
    return " ".join(words[:max_words]).rstrip(",;:") + "."


class SpeechPolicy:
    def __init__(self, max_words=MAX_WORDS, quiet=False):
        self.max_words = max_words
        self.quiet = quiet
        self._explained = set()

    def set_quiet(self, on):
        """Quiet mode: nonverbal only, whatever the reason (BUILD_SPEC 9)."""
        self.quiet = bool(on)

    def allowed(self, reason):
        if self.quiet:
            return False
        return reason in MAY_SPEAK

    def apply(self, reply, reason, error_key=None):
        """Enforce the policy. Returns a NEW Reply; never mutates the input.

        A dropped `say` always leaves a beep behind, so the robot still
        acknowledges that it heard something. Silence on purpose and silence
        from a bug look identical from the outside, and only one of them is
        acceptable.
        """
        say = reply.say
        if say is not None:
            if not self.allowed(reason):
                say = None
            elif reason == ERROR and error_key is not None:
                if error_key in self._explained:
                    say = None
                else:
                    self._explained.add(error_key)
        if say is not None:
            say = truncate(say, self.max_words)

        beep = reply.beep
        if beep is None and say is None:
            # Something was going to be communicated and now is not.
            beep = ("neutral" if reason in (ACTION, CHAT)
                    else reply.beep or "neutral")
        return schema.Reply(say=say, beep=beep, gesture=reply.gesture)

    def sentences(self, reply):
        """Split `say` for streaming, per BUILD_SPEC 7.2."""
        if not reply.say:
            return []
        return [s for s in (p.strip() for p in _SENTENCE.split(reply.say)) if s]

    def forget_errors(self):
        """New session: explain each error once again."""
        self._explained.clear()
