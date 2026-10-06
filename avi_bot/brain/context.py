"""What the cloud is told, and what it is never told. BUILD_SPEC 8.2.

ALLOWED: the personality prompt, the date and time, the current activity plus
verified objects as text, the mood label, the next three calendar events, up
to ten matching facts, and the last six turns.

NEVER: images, audio, raw logs, the calendar URL, keys.

The never-list is enforced here, in the one place the prompt is assembled,
rather than left to each caller. Two of those five are not merely private:

  the calendar's secret iCal address IS the credential, so a provider that
  logs a prompt containing it has read access to his calendar indefinitely;

  frames are of his bedroom, and the whole project's privacy rule is that
  they never leave the MSI.

So `build()` takes typed arguments and there is deliberately no free-form
"extra context" parameter to smuggle a frame through. Anything that does get
through is caught by providers.redact() on the way out, but a second line of
defence is not an excuse for the first one being optional.

BUDGET: a free tier gives about 8k input tokens (GitHub Models is the
tightest), so this is assembled shortest-useful-first and truncated at a
character budget rather than hoping it fits.
"""
from . import providers

# Roughly 4 characters per token; 8k tokens is the tightest free limit, and
# the reply plus tool schemas need room too. 6000 characters is a deliberately
# conservative ceiling for the context block alone.
MAX_CHARS = 6000

MAX_FACTS = 10
MAX_EVENTS = 3
MAX_TURNS = 6


def _clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def build(now=None, activity=None, objects=None, mood=None, events=None,
          facts=None, turns=None, personality=None, max_chars=MAX_CHARS):
    """Assemble the context block. Returns a string.

    Sections are added in order of how much they change the answer, so that
    truncation drops the least useful part rather than the last part someone
    happened to append.
    """
    parts = []

    if personality:
        parts.append(_clip(personality, 1200))

    if now is not None:
        parts.append(f"Now: {now:%A %d %B %Y, %H:%M}.")

    if activity:
        parts.append(f"He appears to be: {_clip(activity, 80)}.")

    if objects:
        names = [_clip(o, 30) for o in list(objects)[:8]]
        parts.append("Verified on the desk: " + ", ".join(names) + ".")

    if mood:
        parts.append(f"Your mood: {_clip(mood, 40)}.")

    if events:
        lines = []
        for event in list(events)[:MAX_EVENTS]:
            when = event.get("when") if isinstance(event, dict) else None
            what = event.get("what") if isinstance(event, dict) else str(event)
            lines.append(f"{when} {what}".strip() if when else str(what))
        parts.append("Next up: " + "; ".join(_clip(l, 60) for l in lines) + ".")

    if facts:
        kept = [_clip(f, 140) for f in list(facts)[:MAX_FACTS]]
        parts.append("You know: " + " | ".join(kept))

    if turns:
        recent = list(turns)[-MAX_TURNS:]
        lines = []
        for turn in recent:
            if isinstance(turn, dict):
                who = turn.get("who", "him")
                text = turn.get("text", "")
            else:
                who, text = "him", str(turn)
            lines.append(f"{who}: {_clip(text, 120)}")
        parts.append("Recent:\n" + "\n".join(lines))

    block = "\n".join(p for p in parts if p)
    # Redact before truncating, so a half-cut key cannot survive the cut.
    block = providers.redact(block)
    if len(block) > max_chars:
        block = block[:max_chars - 1].rstrip() + "…"
    return block


def from_store(store, utterance=None, **kwargs):
    """Pull the facts out of a memory.Store, then build.

    Recall is driven by the utterance, so the ten facts that reach the prompt
    are the ten that might bear on what was just said rather than the ten most
    recent.
    """
    facts = store.recall(query=utterance, limit=MAX_FACTS) if store else None
    return build(facts=facts, **kwargs)
