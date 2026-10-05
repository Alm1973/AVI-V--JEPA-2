"""Cheap OpenCV trigger: decides when the scene changed and has settled.

Runs on every loop frame on a tiny grayscale copy, so it costs almost nothing.
It waits for the scene to go still before firing, so the VLM sees you sitting
down, not a blur halfway through it.
"""
import cv2
import numpy as np

import settings


class ChangeDetector:
    def __init__(self, size=settings.CHANGE_SIZE,
                 pixel_diff=settings.CHANGE_PIXEL_DIFF,
                 change_fraction=settings.CHANGE_FRACTION,
                 settle_fraction=settings.SETTLE_FRACTION,
                 settle_s=settings.SETTLE_S,
                 force_after_s=settings.FORCE_AFTER_S):
        self.size = size
        self.pixel_diff = pixel_diff
        self.change_fraction = change_fraction
        self.settle_fraction = settle_fraction
        self.settle_s = settle_s
        self.force_after_s = force_after_s
        self.ref = None
        self.prev = None
        self.changing_since = None
        self.still_since = None
        self.suppress_until = 0.0
        self.last_reason = None

    def _prep(self, frame):
        small = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        if small.ndim == 3:
            small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(small, (5, 5), 0)

    def _fraction(self, a, b):
        return float(np.mean(cv2.absdiff(a, b) > self.pixel_diff))

    def suppress(self, until):
        """Ignore changes until this time, e.g. while the arm is moving itself."""
        self.suppress_until = max(self.suppress_until, until)

    def reset(self, frame):
        """Make this frame the new reference (call after every observation)."""
        small = self._prep(frame)
        self.ref = small
        self.prev = small
        self.changing_since = None
        self.still_since = None

    def update(self, frame, now):
        """Returns True once when a change has happened and the scene has settled
        (or has kept moving for FORCE_AFTER_S)."""
        small = self._prep(frame)
        if self.ref is None or now < self.suppress_until:
            self.ref = small
            self.prev = small
            self.changing_since = None
            self.still_since = None
            return False

        motion = self._fraction(small, self.prev)
        self.prev = small

        if self.changing_since is None:
            if self._fraction(small, self.ref) > self.change_fraction:
                self.changing_since = now
                self.still_since = None
            return False

        if motion < self.settle_fraction:
            if self.still_since is None:
                self.still_since = now
            if now - self.still_since >= self.settle_s:
                if self._fraction(small, self.ref) <= self.change_fraction:
                    # Something passed through and the scene went back to how
                    # it was (a hand waved past). Nothing new to look at.
                    self.changing_since = None
                    self.still_since = None
                    return False
                self.last_reason = "change"
                self._finish(small)
                return True
        else:
            self.still_since = None

        if now - self.changing_since >= self.force_after_s:
            self.last_reason = "change-forced"
            self._finish(small)
            return True
        return False

    def _finish(self, small):
        self.ref = small
        self.changing_since = None
        self.still_since = None
