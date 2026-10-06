"""sleep / wake / quiet / guest_mode. BUILD_SPEC 9.

Four switches that change what the rest of the robot is allowed to do, kept in
one place so there is a single answer to "is it allowed to log this" rather
than a flag checked in four modules that disagree.

GUEST MODE FAILS CLOSED. If anything about it is uncertain, logging stops.
Someone else is in the room and the camera is pointed at them; the cost of
missing an hour of observations is nothing, and the cost of recording a guest
who did not agree to it is the kind of mistake that cannot be undone.
"""
import time

REST = "REST"


class Modes:
    def __init__(self, clock=time.time, on_pose=None, on_logging=None,
                 policy=None):
        self.clock = clock
        # Injected so the switches are testable with no arm and no observer.
        self.on_pose = on_pose or (lambda name: None)
        self.on_logging = on_logging or (lambda enabled: None)
        self.policy = policy            # brain.speech.SpeechPolicy, optional
        self.asleep = False
        self.guest = False
        self.quiet_until = 0.0

    # --- sleep / wake ----------------------------------------------------
    def sleep(self):
        if self.asleep:
            return None, {"asleep": True}
        self.asleep = True
        self.on_pose(REST)
        self.on_logging(False)
        return None, {"asleep": True}

    def wake(self):
        if not self.asleep:
            return None, {"asleep": False}
        self.asleep = False
        self.on_pose("HOME")
        # Logging resumes only if a guest is not present: waking up must not
        # quietly undo guest mode.
        self.on_logging(not self.guest)
        return None, {"asleep": False}

    # --- quiet -----------------------------------------------------------
    def quiet(self, minutes=None):
        """Nonverbal only. Indefinite if no length is given."""
        minutes = float(minutes) if minutes else None
        self.quiet_until = (self.clock() + minutes * 60.0 if minutes
                            else float("inf"))
        if self.policy is not None:
            self.policy.set_quiet(True)
        return None, {"quiet": True, "minutes": minutes}

    def unquiet(self):
        self.quiet_until = 0.0
        if self.policy is not None:
            self.policy.set_quiet(False)
        return None, {"quiet": False}

    @property
    def is_quiet(self):
        was = self.clock() < self.quiet_until
        if not was and self.policy is not None and self.policy.quiet:
            # The timer ran out; let the policy speak again without needing
            # anyone to call unquiet().
            self.policy.set_quiet(False)
        return was

    # --- guest -----------------------------------------------------------
    def guest_mode(self, on=None):
        """Toggle unless told. Logging stops; nothing is written."""
        self.guest = (not self.guest) if on is None else bool(on)
        self.on_logging(not self.guest and not self.asleep)
        return None, {"guest": self.guest}

    @property
    def may_log(self):
        """The single answer to 'is it allowed to record this'."""
        return not self.guest and not self.asleep

    def snapshot(self):
        return {"asleep": self.asleep, "guest": self.guest,
                "quiet": self.is_quiet, "may_log": self.may_log}


def make(modes):
    return {
        "sleep": lambda: modes.sleep(),
        "wake": lambda: modes.wake(),
        "quiet": lambda minutes=None: modes.quiet(minutes),
        "guest_mode": lambda: modes.guest_mode(),
    }
