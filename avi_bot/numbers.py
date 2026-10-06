"""Spoken numbers to integers. One home, because two callers need it.

Speech-to-text writes numbers as WORDS. "Focus for twenty five minutes"
arrives from faster-whisper exactly like that, never as "25", so any command
parser that only understands digits works when typed and fails when spoken --
which is the harder bug to notice, because the typed path is the one that gets
tested by hand.

Found 2026-10-06 by routing the TTS audio fixtures through the local router:
"twenty five" did not parse, and the focus command silently lost its duration
and started an open-ended session instead of a 25-minute one.

Deliberately small. It handles what someone says to a desk robot -- up to
ninety-nine, plus a few bare words -- and returns None for everything else so
the caller can refuse rather than guess.
"""
import re

ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40,   # common misspell
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
# Words that stand for a quantity without being a number.
QUANTIFIERS = {"a": 1, "an": 1, "couple": 2, "few": 3, "half": 0}

_JOINER = re.compile(r"[\s-]+")


def words_to_int(text, allow_quantifiers=False):
    """'twenty five' -> 25, 'ninety' -> 90, '25' -> 25. None if unclear.

    Hyphens and spaces are both accepted because transcripts use either
    ("twenty-five" and "twenty five" are the same utterance).
    """
    if text is None:
        return None
    t = " ".join(str(text).split()).strip().lower()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    parts = [p for p in _JOINER.split(t) if p]
    if not parts:
        return None
    if len(parts) == 1:
        word = parts[0]
        if word in ONES:
            return ONES[word]
        if word in TENS:
            return TENS[word]
        if allow_quantifiers and word in QUANTIFIERS:
            return QUANTIFIERS[word]
        return None
    if len(parts) == 2:
        tens, ones = parts
        # "twenty five" only. Not "five twenty", which is a time, and not
        # "twenty twenty", which is a year -- both would be wrong answers
        # dressed up as right ones.
        if tens in TENS and ones in ONES and 1 <= ONES[ones] <= 9:
            return TENS[tens] + ONES[ones]
        return None
    return None
