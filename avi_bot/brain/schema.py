"""The brain's output contract. BUILD_SPEC 8.2.

Every reply, from any provider, is exactly this:

    {"say": str|null, "beep": emotion|null, "gesture": name|null}

`say` and `beep` are SEPARATE on purpose, and the separation is the whole
point of the speech policy: the brain proposes words, and `speech.py` decides
whether words were allowed at all. A model that returns prose for "lamp off"
should have that prose dropped, not obeyed, and that is only possible if the
chirp is carried in its own field rather than implied by the absence of text.

Validation is strict and the failure path is defined: invalid output is retried
once and then becomes a confused chirp (BUILD_SPEC 8.1). It is never patched up
into something plausible, because a silently repaired reply is how a model's
misunderstanding turns into an action nobody asked for.
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

# The emotions beeps.py can actually synthesise. Kept here as the contract's
# own list so a model naming something else is a validation error rather than
# a silent no-sound.
EMOTIONS = ("neutral", "happy", "excited", "curious", "sad", "sulky",
            "confused", "alert", "sleepy", "affectionate", "proud")

# What the brain may ask the body to do. Deliberately a subset of the gesture
# library: the LLM gets expressive moves, not calibration or self-test moves.
GESTURES = ("nod", "shake", "tilt", "perk_up", "droop", "shrug", "lean_in",
            "look_around", "celebrate", "ahem", "yawn_wake", "thinking",
            "embarrassed", "greet", "glance_at_you", "fidget", "still",
            "breathe", "curious_lean",
            # The model is told play_emotion can wave and puts it in the
            # reply too; refused here, "good job avi" became a head shake.
            "wave")

MAX_WORDS = 20          # BUILD_SPEC 7.2
CONFUSED = "confused"


class Reply(BaseModel):
    """One turn's worth of output. Nothing else is accepted."""

    model_config = ConfigDict(extra="forbid")

    say: Optional[str] = None
    beep: Optional[str] = None
    gesture: Optional[str] = None

    @field_validator("say")
    @classmethod
    def _clean_say(cls, v):
        if v is None:
            return None
        v = " ".join(v.split())
        return v or None

    @field_validator("beep")
    @classmethod
    def _known_beep(cls, v):
        if v is None:
            return None
        v = v.strip().lower()
        if v not in EMOTIONS:
            raise ValueError(f"unknown beep {v!r}; have {list(EMOTIONS)}")
        return v

    @field_validator("gesture")
    @classmethod
    def _known_gesture(cls, v):
        if v is None:
            return None
        v = v.strip().lower()
        if v not in GESTURES:
            raise ValueError(f"unknown gesture {v!r}")
        return v

    @property
    def words(self):
        return len(self.say.split()) if self.say else 0

    @property
    def silent(self):
        return not (self.say or self.beep or self.gesture)


def confused_reply():
    """The defined failure. BUILD_SPEC 8.1: a confused chirp plus a shake."""
    return Reply(say=None, beep=CONFUSED, gesture="shake")


def parse(raw):
    """Validate a provider's output. Returns (Reply, error_or_None).

    Never raises: the caller's job is to retry once and then fall back, and a
    raising validator in the middle of that makes the retry logic the error
    handler's problem instead of the router's.
    """
    import json

    if isinstance(raw, Reply):
        return raw, None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        # Models fence JSON even when told not to.
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        try:
            raw = json.loads(text)
        except Exception as exc:
            return None, f"not JSON: {exc}"
    if not isinstance(raw, dict):
        return None, f"expected an object, got {type(raw).__name__}"
    try:
        return Reply(**raw), None
    except Exception as exc:
        # Keep the detail. pydantic's first line is only "N validation errors
        # for Reply", which says nothing about WHICH field or what value, and
        # that message ends up in the interactions log as the whole record of
        # why a reply was thrown away.
        lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
        return None, "; ".join(lines[1:] or lines) or str(exc)
