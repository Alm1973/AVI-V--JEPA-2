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

    def __init__(self, port, config, baud=115200, open_now=False,
                 retry_s=3.0):
        self.port = port
        self.config = config
        self.baud = baud
        self.ser = None
        # A USB serial handle can go bad while the device itself is perfectly
        # fine -- a re-enumeration is enough. Measured on 2026-10-05: a write
        # raised "WriteFile failed (PermissionError(13, 'The device does not
        # recognize the command.'))" while Windows still listed "Arduino Uno
        # (COM5)" as Status OK. The caller's loop had no handler, so one
        # transient error killed it silently: the page stayed up, the firmware
        # held position exactly as its watchdog should, and nothing moved
        # again. Recovering belongs here, where every caller gets it.
        self.healthy = False
        self.last_error = None
        self.reopens = 0
        self.retry_s = float(retry_s)
        self._next_retry = 0.0
        # Reconnecting may only ever RE-open a port that was opened on purpose.
        # Without this, send() on a never-opened backend would open COM5 by
        # itself, which resets the Uno and moves the servos -- the exact
        # surprise that CLAUDE.md says must never happen without a go-ahead.
        self._was_opened = False
        if open_now:
            self.open()

    def open(self):
        """Open the port. NOTE: this asserts DTR, which reboots the Uno."""
        import serial  # imported late: the Mac has no pyserial and needs none
        self.ser = serial.Serial(self.port, self.baud, timeout=0.3)
        # Opening the port asserts DTR, which reboots the Uno. The bootloader
        # waits before handing over, so 2 s was marginal: measured 3 s from
        # open to the sketch's banner.
        time.sleep(3.0)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self._was_opened = True
        self.healthy = True
        self.last_error = None
        return self

    def _write(self, line):
        if self.ser is None:
            raise RuntimeError("serial port is not open; call open() first")
        self.ser.write((line + "\n").encode("ascii"))

    def _reopen(self):
        """Try to get the port back, no more often than retry_s.

        Refuses outright if the port has never been opened: recovering a lost
        connection is not the same thing as making one.
        """
        if not self._was_opened:
            return False
        now = time.time()
        if now < self._next_retry:
            return False
        self._next_retry = now + self.retry_s
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        try:
            self.open()
        except Exception as exc:
            self.last_error = f"reopen failed: {exc}"
            return False
        self.reopens += 1
        self.healthy = True
        self.last_error = None
        log.warning("serial port %s reopened (%d time(s) so far)",
                    self.port, self.reopens)
        return True

    def send(self, pose, t=None):
        """Send a target, surviving a port that has gone away.

        Returns True if the frame went out. A dropped frame is harmless on its
        own -- the host repeats the target continuously, and the firmware holds
        position if it hears nothing for 500 ms.
        """
        if self.ser is None and not self._reopen():
            return False
        try:
            self._write("P " + " ".join(f"{pose[j]:.1f}" for j in poses.JOINTS))
            self.healthy = True
            return True
        except Exception as exc:
            if self.healthy or self.last_error is None:
                log.error("serial write failed, will reconnect: %s", exc)
            self.healthy = False
            self.last_error = str(exc)
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            return False

    def home(self):
        self._write("H")

    def calibrate(self, channel, deg):
        """Drive one channel even if it is marked undriven (firmware `C`).

        This is how an unknown direction sign gets discovered: there is no other
        way to learn which way a joint goes than to move it a little and look.
        The firmware clamps to the joint's soft limits and only honours it for a
        few seconds.
        """
        self._write(f"C {int(channel)} {float(deg):.1f}")

    def set_speed_cap(self, deg_s):
        """Set the firmware's speed cap at runtime (firmware `V`)."""
        self._write(f"V {float(deg_s):.1f}")

    def hold(self):
        self._write("E")

    def resume(self):
        self._write("R")

    def read_state(self, attempts=4):
        """Ask the firmware where it is. Returns ({joint: deg}, version) or None.

        It reads several lines per attempt rather than one, because the boot
        banner and any stray output share this port: taking the first line that
        comes back would hand the caller a comment and call it a position.
        """
        for _ in range(attempts):
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
            self._write("?")
            for _ in range(6):
                raw = self.ser.readline()
                if not raw:
                    break
                parts = raw.decode("ascii", "replace").strip().split()
                if len(parts) == 6 and parts[0] == "S":
                    try:
                        vals = [float(v) for v in parts[1:5]]
                    except ValueError:
                        continue
                    return dict(zip(poses.JOINTS, vals)), parts[5]
            time.sleep(0.25)
        return None

    def close(self):
        if self.ser is not None:
            self.ser.close()
            self.ser = None
