"""Shortcuts AVI learns from its own turns: the same words, the same actions.

Every command that is not one of the hand-written quick rules (intents.py)
goes to the cloud model: a second or two, ~2.3K tokens of a free daily
budget, and nothing at all when that budget is gone. But much of what he
says, he says again -- and when the model answers the same words with the
same actions twice, the round trip has stopped teaching AVI anything.

So AVI keeps the words and the actions (Voyager's skill library, applied to
language: docs/RESEARCH-motion.md). A shortcut is PROVEN when the model has
produced the identical actions for it twice, or when he praised what it
did; a proven shortcut is used straight away -- instant, free, and working
offline. One seen only once is used only when the model cannot be reached.
A scold right after a shortcut drops it.

What is never learned: words that point at the conversation ("do that
again", "turn it off") -- the same words mean different things each time --
and tools whose arguments are his own content (remember, forget, open_app).
A performance is learned by its name only, so the shortcut replays the kept
dance rather than one frozen copy of the moves.
"""
import json
import re

# Tools a shortcut may call: ones whose arguments come from the words.
SAFE = {"perform", "play_emotion", "nudge", "look_at", "look_around",
        "describe_view", "go_to_pose", "find_object", "get_time", "weather",
        "list_timers", "set_timer", "cancel_timer", "focus_start",
        "focus_stop", "focus_status", "sleep", "wake", "quiet", "media",
        "what_do_you_know", "robot_status", "get_schedule", "guest_mode",
        "lamp", "set_reminder", "move_joints", "motion_sequence"}

# Words that refer back to the conversation or the moment.
_POINTING = {"it", "that", "this", "these", "those", "them", "again",
             "there", "yes", "no", "ok", "okay", "yeah", "nope", "same",
             "more", "less", "back", "instead"}

_LEAD = re.compile(r"^(?:hey\s+)?(?:avi|jarvis)\b[,\s]*")


def key(utterance):
    """The words, normalised: lower case, no punctuation, no "hey avi"."""
    t = " ".join((utterance or "").lower().split())
    t = _LEAD.sub("", t)
    t = "".join(c if c.isalnum() or c in " '" else " " for c in t)
    return " ".join(t.split())


def learnable(utterance, calls):
    k = key(utterance)
    if not k or not calls or len(k) > 120:
        return False
    if set(k.split()) & _POINTING:
        return False
    return all(name in SAFE for name, _ in calls)


def _canonical(calls):
    """Calls as kept: a performance by its name only."""
    out = []
    for name, args in calls:
        args = dict(args or {})
        if name == "perform":
            args = {k: args[k] for k in ("name", "adjust") if args.get(k)}
        out.append([name, args])
    return out


class Shortcuts:
    PROVEN_AFTER = 2          # identical model answers before it is trusted
    FEEDBACK_WINDOW_S = 120.0
    MAX_KEPT = 200

    def __init__(self, store, clock=None):
        self.store = store
        self.db = store.db
        self.clock = clock or store.clock
        self.last = None          # (key, ts, used_shortcut) of the last turn

    def lookup(self, utterance):
        """(calls, proven) for these words, or None."""
        r = self.db.execute("SELECT calls, seen, proven FROM shortcuts"
                            " WHERE key=?", (key(utterance),)).fetchone()
        if r is None:
            return None
        calls = [(n, a) for n, a in json.loads(r["calls"])]
        return calls, bool(r["proven"]) or r["seen"] >= self.PROVEN_AFTER

    def used(self, utterance):
        k = key(utterance)
        self.db.execute("UPDATE shortcuts SET uses = uses + 1, last_ts = ?"
                        " WHERE key = ?", (self.clock(), k))
        self.db.commit()
        self.last = (k, self.clock(), True)

    def observe(self, utterance, calls):
        """The model answered `utterance` with `calls`: count it if the same
        as last time, start over if different."""
        if any(name in ("praise", "scold") for name, _ in calls or []):
            return                # about the turn before; feedback() reads it
        if not learnable(utterance, calls):
            self.last = (key(utterance), self.clock(), False)
            return
        k = key(utterance)
        canon = json.dumps(_canonical(calls), sort_keys=True)
        now = self.clock()
        with self.db.lock:
            r = self.db.execute("SELECT calls FROM shortcuts WHERE key=?",
                                (k,)).fetchone()
            if r is not None and r["calls"] == canon:
                self.db.execute("UPDATE shortcuts SET seen = seen + 1,"
                                " last_ts = ? WHERE key = ?", (now, k))
            else:
                self.db.execute(
                    "INSERT INTO shortcuts (key, calls, seen, proven,"
                    " created_at, last_ts) VALUES (?,?,1,0,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET calls=excluded.calls,"
                    " seen=1, proven=0, last_ts=excluded.last_ts",
                    (k, canon, now, now))
                n = self.db.execute(
                    "SELECT COUNT(*) FROM shortcuts").fetchone()[0]
                if n > self.MAX_KEPT:
                    self.db.execute(
                        "DELETE FROM shortcuts WHERE key IN (SELECT key FROM"
                        " shortcuts ORDER BY proven ASC, uses ASC, last_ts"
                        " ASC LIMIT ?)", (n - self.MAX_KEPT,))
            self.db.commit()
        self.last = (k, now, False)

    def feedback(self, good):
        """Praise proves the last turn's shortcut; a scold drops it."""
        if self.last is None:
            return None
        k, ts, _ = self.last
        if self.clock() - ts > self.FEEDBACK_WINDOW_S:
            return None
        if good:
            cur = self.db.execute("UPDATE shortcuts SET proven = 1"
                                  " WHERE key = ?", (k,))
        else:
            cur = self.db.execute("DELETE FROM shortcuts WHERE key = ?",
                                  (k,))
        self.db.commit()
        self.last = None
        return k if cur.rowcount else None

    def known(self, limit=20):
        return [dict(r) for r in self.db.execute(
            "SELECT key, calls, seen, proven, uses FROM shortcuts"
            " ORDER BY proven DESC, uses DESC, seen DESC LIMIT ?",
            (int(limit),))]
