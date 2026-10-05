"""Where a pose actually goes: the arm, the sim, or a log line.

BUILD_SPEC 6.5. Every gesture runs through SimBackend before it is ever allowed
near SerialBackend.
"""
import logging
import time

from . import poses

log = logging.getLogger("avi.motion")


class MotionBackend:
    name = "base"

    def send(self, pose, t=None):
        raise NotImplementedError

    def home(self):
        """Ask for a slow move home. The arm does this itself, in firmware."""

    def read_state(self):
        return None

    def close(self):
        pass


class DryRunBackend(MotionBackend):
    """Logs only. Used for --dry-run and for tests that assert on commands."""
    name = "dry-run"

    def __init__(self, logger=None, every=1):
        self.log = logger or log
        self.every = every
        self.commands = []
        self._n = 0

    def send(self, pose, t=None):
        self.commands.append((t if t is not None else time.time(), dict(pose)))
        self._n += 1
        if self._n % self.every == 0:
            self.log.debug("P %s", " ".join(
                f"{pose[j]:.1f}" for j in poses.JOINTS))

    def home(self):
        self.log.info("H (home)")


class SimBackend(MotionBackend):
    """Keeps joint state so the dashboard can draw it. No hardware, no camera.

    Holds a bounded history so the dashboard can show a trace and so a test can
    assert that a 30-minute run stayed inside its limits without the memory
    growing without bound.
    """
    name = "sim"

    def __init__(self, config, history=4096):
        self.config = config
        self.state = poses.get_pose("HOME")
        self.history = []
        self._cap = history
        self.sent = 0

    def send(self, pose, t=None):
        self.state = dict(pose)
        self.sent += 1
        self.history.append((t if t is not None else time.time(), dict(pose)))
        if len(self.history) > self._cap:
            del self.history[:len(self.history) - self._cap]

    def counts(self):
        """The PWM counts the firmware would produce, for the dashboard."""
        return {j: round(self.config.deg_to_count(j, self.state[j]))
                for j in poses.JOINTS}

    def home(self):
        self.state = poses.get_pose("HOME")


class SerialBackend(MotionBackend):
    """The real arm. Speaks the firmware v2 protocol from BUILD_SPEC 6.4.

    Opening the port RESETS the Uno, so constructing this class is a physical
    event: the servos may move. It therefore does not open the port on import or
    on construction-by-default anywhere in the codebase, and CLAUDE.md says not
    to open COM5 without Shaurya's explicit go-ahead.
    """
    name = "serial"

    def __init__(self, port, config, baud=115200, open_now=False):
        self.port = port
        self.config = config
        self.baud = baud
        self.ser = None
        if open_now:
            self.open()

    def open(self):
        import serial  # imported late: the Mac has no pyserial and needs none
        self.ser = serial.Serial(self.port, self.baud, timeout=0.2)
        # The Uno reboots when the port opens; its bootloader eats early bytes.
        time.sleep(2.0)
        return self

    def _write(self, line):
        if self.ser is None:
            raise RuntimeError("serial port is not open; call open() first")
        self.ser.write((line + "\n").encode("ascii"))

    def send(self, pose, t=None):
        self._write("P " + " ".join(f"{pose[j]:.1f}" for j in poses.JOINTS))

    def home(self):
        self._write("H")

    def hold(self):
        self._write("E")

    def resume(self):
        self._write("R")

    def read_state(self):
        self._write("?")
        line = self.ser.readline().decode("ascii", "replace").strip()
        parts = line.split()
        if len(parts) != 6 or parts[0] != "S":
            return None
        return dict(zip(poses.JOINTS, (float(p) for p in parts[1:5]))), parts[5]

    def close(self):
        if self.ser is not None:
            self.ser.close()
            self.ser = None
