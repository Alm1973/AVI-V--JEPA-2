"""Offer a break after a long stretch at the desk -- at a pause, never mid-thought.

The first behaviour that acts on something AVI learned by watching rather than
something it was told. focus.py already offers a break after 50 minutes, but
only inside a focus session he has to start by command, and its breakpoints
come from change.py running in the observer -- which cannot share the camera
with the dashboard. So on the robot it never fired.

Presence removes the need to be told: AVI can SEE that he has been at the desk
for 50 minutes. What it must not do is act on the stopwatch. focus.py's own
design, from the interruption literature: elapsed time is evidence, not a
trigger, and a badly timed interruption raises the chance he ignores AVI from
then on. So the clock only makes him eligible; the trigger is a breakpoint.

And not every breakpoint. change.py fires for two reasons: "change" -- motion
that then SETTLED, which is what a natural pause looks like -- and
"change-forced" -- motion that went on too long, which is someone mid-activity.
Only the first counts here.

WHETHER HE TAKES THEM
An offer he ignores is information. Each ignored offer in a row pushes the
next one BACKOFF_S later, up to MAX_BACKOFF_STEPS; one he takes resets it.
So a coach that offers at the wrong moment learns to wait, instead of being
the thing that teaches him to ignore AVI (presence.break_outcomes judges each
offer from whether he left the desk soon after).
"""
import time

from . import focus as focus_mod

BREAK = "break"
NATURAL_PAUSE = "change"
BACKOFF_S = 900.0              # 15 minutes later per ignored offer
MAX_BACKOFF_STEPS = 3          # 50 min at most becomes 95


class BreakCoach:
    def __init__(self, presence, clock=time.time,
                 after_s=focus_mod.BREAK_AFTER_S):
        self.presence = presence
        self.clock = clock
        self.after_s = float(after_s)
        self.offered_at = None
        self._session = None

    def on_breakpoint(self, reason, now=None):
        """A breakpoint arrived. Returns BREAK if this is the moment, else None.

        Eligible only when he is here, has been here `after_s`, and the
        breakpoint is a settled pause. One offer per `after_s` of desk time,
        so a long session can be offered again but a pause every ten seconds
        cannot become a nag.
        """
        now = self.clock() if now is None else now
        p = self.presence
        if not p.here or p.since is None:
            return None
        if reason != NATURAL_PAUSE:
            return None
        if self._session != p.since:            # a new session resets offers
            self._session, self.offered_at = p.since, None
        anchor = self.offered_at if self.offered_at is not None else p.since
        if now - anchor < self.interval(now):
            return None
        self.offered_at = now
        return BREAK

    def ignored_in_a_row(self, now=None):
        """How many of the latest judged offers in a row he ignored."""
        judge = getattr(self.presence, "break_outcomes", None)
        if judge is None:
            return 0
        n = 0
        for _, outcome in reversed(judge(now=now)):
            if outcome != "ignored":
                break
            n += 1
        return n

    def interval(self, now=None):
        """Desk time before an offer, lengthened by ignored offers."""
        steps = min(self.ignored_in_a_row(now), MAX_BACKOFF_STEPS)
        return self.after_s + steps * BACKOFF_S

    def status(self, now=None):
        now = self.clock() if now is None else now
        p = self.presence
        if not p.here or p.since is None:
            return {"eligible_in_min": None}
        anchor = (self.offered_at if self._session == p.since
                  and self.offered_at is not None else p.since)
        interval = self.interval(now)
        left = max(0.0, interval - (now - anchor))
        return {"eligible_in_min": round(left / 60.0, 1),
                "waiting_for_a_pause": left == 0.0,
                "offer_after_min": round(interval / 60.0)}
