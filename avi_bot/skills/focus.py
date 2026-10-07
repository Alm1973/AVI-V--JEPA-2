"""Focus mode: a quiet companion that nudges at the right moment or not at all.

BUILD_SPEC 9, as amended 2026-10-06 after reading the interruption
literature (docs/RESEARCH-NOTES.md §3).

THE TRIGGER IS NOT A STOPWATCH, AND THAT IS THE WHOLE DESIGN
The original spec said "phone use over 2 min -> an ahem". That is a
time-based trigger, and those are specifically what the research finds
unreliable: elapsed time and idleness are poor indicators of readiness,
because a pause may be planning or reading rather than distraction. Worse, a
badly timed interruption measurably raises the chance the person ignores the
system from then on -- so a mistimed `ahem` does not fail once, it trains him
to disregard AVI entirely.

So phone time is EVIDENCE and a breakpoint is the TRIGGER. Both must hold.
`change.py` already detects breakpoints (the scene changed and then settled),
which is what makes this nearly free to implement.

If no breakpoint ever arrives, AVI says nothing and the end-of-session summary
carries it. Saying nothing is a correct outcome here, not a failure.
"""
import time

PHONE_EVIDENCE_S = 120.0       # phone time before a nudge is even considered
MAX_NUDGES = 2                 # per session, BUILD_SPEC 9
BREAK_AFTER_S = 3000.0         # suggest a break at 50 minutes
NUDGE_GAP_S = 300.0            # never two nudges within five minutes

AHEM = "ahem"                  # first nudge: glance + chirp, no words
WORDS = "words"                # second nudge: one short sentence
BREAK = "break"


class Session:
    __slots__ = ("started", "minutes", "phone_s", "nudges", "last_nudge_at",
                 "break_offered", "ended", "distracted_since")

    def __init__(self, started, minutes=None):
        self.started = started
        self.minutes = minutes
        self.phone_s = 0.0
        self.nudges = 0
        self.last_nudge_at = None
        self.break_offered = False
        self.ended = None
        self.distracted_since = None


class Focus:
    def __init__(self, clock=time.time, phone_evidence_s=PHONE_EVIDENCE_S,
                 max_nudges=MAX_NUDGES, break_after_s=BREAK_AFTER_S,
                 on_change=None):
        self.clock = clock
        # on_change("focus_start" | "focus_end"): the mood's focus flag, which
        # is what quiets idle behaviour during a session. Called from start
        # and stop, so a session ended by the clock counts too.
        self.on_change = on_change
        self.phone_evidence_s = phone_evidence_s
        self.max_nudges = max_nudges
        self.break_after_s = break_after_s
        self.session = None
        self.history = []

    # --- lifecycle -------------------------------------------------------
    def start(self, minutes=None):
        now = self.clock()
        if self.session is not None and self.session.ended is None:
            return "Already focusing.", None
        self.session = Session(now, minutes)
        self.history.append(self.session)
        self._changed("focus_start")
        return (f"Focusing for {int(minutes)}." if minutes
                else "Focusing."), None

    def stop(self):
        """End the session and summarise it. Spoken, per BUILD_SPEC 9."""
        if self.session is None or self.session.ended is not None:
            return "Not focusing.", None
        s = self.session
        s.ended = self.clock()
        total = s.ended - s.started
        self.session = None
        self._changed("focus_end")
        mins = int(total // 60)
        phone = int(s.phone_s // 60)
        data = {"minutes": mins, "phone_minutes": phone, "nudges": s.nudges}
        if phone <= 0:
            return f"{mins} minutes, no phone. Good one.", data
        return f"{mins} minutes, {phone} on your phone.", data

    def _changed(self, event):
        if self.on_change is not None:
            try:
                self.on_change(event)
            except Exception:
                pass                  # a mood hiccup never blocks focus

    def status(self):
        if self.session is None or self.session.ended is not None:
            return "Not focusing.", None
        s = self.session
        elapsed = int((self.clock() - s.started) // 60)
        if s.minutes:
            left = max(0, int(s.minutes) - elapsed)
            return f"{left} minutes left.", {"elapsed": elapsed, "left": left}
        return f"{elapsed} minutes in.", {"elapsed": elapsed}

    @property
    def active(self):
        return self.session is not None and self.session.ended is None

    # --- observing -------------------------------------------------------
    def saw_phone(self, on, now=None):
        """Called with each phone verdict. Accumulates EVIDENCE only.

        Nothing is ever triggered from here, which is the point: this is the
        measurement, and `tick` is the decision.
        """
        if not self.active:
            return
        now = self.clock() if now is None else now
        s = self.session
        if on:
            if s.distracted_since is None:
                s.distracted_since = now
            else:
                s.phone_s += now - s.distracted_since
                s.distracted_since = now
        else:
            s.distracted_since = None

    def tick(self, now=None, breakpoint=False):
        """Decide whether to act. Returns a nudge kind, or None.

        `breakpoint` comes from change.py: the scene changed and then settled.
        Nothing is said without one, however much phone time has accumulated.
        """
        if not self.active:
            return None
        now = self.clock() if now is None else now
        s = self.session

        # A break suggestion is about elapsed effort, not distraction, and it
        # still waits for a breakpoint -- interrupting someone mid-thought to
        # suggest a rest is the exact failure this module is built around.
        if (not s.break_offered and breakpoint
                and now - s.started >= self.break_after_s):
            s.break_offered = True
            s.last_nudge_at = now
            return BREAK

        if s.phone_s < self.phone_evidence_s:
            return None
        if s.nudges >= self.max_nudges:
            return None
        if not breakpoint:
            return None
        if s.last_nudge_at is not None and \
                now - s.last_nudge_at < NUDGE_GAP_S:
            return None
        s.nudges += 1
        s.last_nudge_at = now
        # Reset the evidence: the nudge has been spent on this stretch of
        # distraction, and carrying it forward would fire again at the next
        # breakpoint for the same phone time.
        s.phone_s = 0.0
        return AHEM if s.nudges == 1 else WORDS

    def phone_minutes(self):
        return round((self.session.phone_s if self.active else 0.0) / 60.0, 1)


def make(focus):
    """Handlers for Registry.register."""
    return {
        "focus_start": lambda minutes=None: focus.start(minutes),
        "focus_stop": lambda: focus.stop(),
        "focus_status": lambda: focus.status(),
    }
