"""remember / forget / what_do_you_know. BUILD_SPEC 9.

Thin wrappers over memory.Store, and the thinness is the point: the limits
(100 facts, 280 characters, citation-weighted eviction) belong in one place,
and a skill layer that re-implemented any of them would drift from it.

Everything returns something sayable, because all three of these are things he
asked about and so are allowed words under the speech policy.
"""


def make(store):
    """Returns {tool_name: handler} ready for Registry.register."""

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
        if not facts:
            return ("Nothing about that." if query else "Nothing yet."), None
        # The speech cap is 20 words, so list rather than recite: two facts is
        # already most of the budget.
        head = facts[0]
        if len(facts) == 1:
            return head, {"facts": facts}
        return f"{head} And {len(facts) - 1} more.", {"facts": facts}

    return {"remember": remember, "forget": forget,
            "what_do_you_know": what_do_you_know}
