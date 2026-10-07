"""remember / forget / what_do_you_know. BUILD_SPEC 9.

Thin wrappers over memory.Store, and the thinness is the point: the limits
(100 facts, 280 characters, citation-weighted eviction) belong in one place,
and a skill layer that re-implemented any of them would drift from it.

Everything returns something sayable, because all three of these are things he
asked about and so are allowed words under the speech policy.
"""


def make(store, observed=None):
    """Returns {tool_name: handler} ready for Registry.register.

    `observed`, when given, returns sentences AVI noticed by watching
    (perception/observations.py) -- the half of "what do you know about me"
    he never had to tell it.
    """

    def remember(text):
        try:
            _, dropped = store.remember(text)
        except ValueError as exc:
            return str(exc), None
        if dropped:
            # Say so. Silently forgetting something to make room is the kind
            # of thing that makes a memory feel untrustworthy.
            return f"Noted. Forgot: {dropped[:60]}", {"dropped": dropped}
        return "Noted.", {"dropped": None}

    def forget(text):
        n = store.forget(text)
        return ("Forgotten." if n else "I didn't have that."), {"removed": n}

    def what_do_you_know(query=None):
        facts = store.recall(query=query, limit=5)
        seen = []
        if observed is not None:
            try:
                from avi_bot.perception import observations
                seen = observations.about(query, observed() or [])
            except Exception:
                seen = []              # watching failed; what he said stands
        data = {"facts": facts, "noticed": seen}
        if not facts and not seen:
            return ("Nothing about that." if query else "Nothing yet."), None
        # The speech cap is 20 words, so one of each, then a count: two
        # sentences is already most of the budget.
        parts = ([facts[0]] if facts else []) + ([seen[0]] if seen else [])
        rest = max(0, len(facts) - 1) + max(0, len(seen) - 1)
        text = " ".join(p.rstrip(".") + "." for p in parts)
        if rest:
            text += f" And {rest} more."
        return text, data

    return {"remember": remember, "forget": forget,
            "what_do_you_know": what_do_you_know}
