"""Host-side joint clamp and slew limiter.

The firmware enforces the same table independently (BUILD_SPEC 6.4). This is
the second of the two fences, not the only one: if this module is wrong the arm
is still safe, and if the serial link corrupts a frame the firmware is still
safe. Neither layer trusts the other.
"""
from . import poses


class Limiter:
    """Clamps a requested pose and limits how fast it may change.

    `last` is the pose actually emitted on the previous tick, which is what the
    slew limit is measured against -- not the requested pose. Measuring against
    the request would let a long run of clamped requests accumulate into a jump
    the moment the clamp released.
    """

    def __init__(self, config, start=None, speed_cap=None,
                 drive_undriven=False):
        self.config = config
        self.speed_cap = config.speed_cap if speed_cap is None else float(speed_cap)
        # Undriven joints are pinned to center by default, because their
        # direction sign is unknown and a wrong sign drives them into an
        # end-stop. Set True ONLY in the sim, to preview what a gesture will
        # look like once M1 has measured the signs. Never True on hardware.
        self.drive_undriven = bool(drive_undriven)
        self.last = dict(start or poses.get_pose("HOME"))
        self.clamped_joints = set()   # joints hit by the last call, for logging

    def reset(self, pose):
        self.last = dict(pose)

    def apply(self, requested, dt):
        """Return the pose to emit this tick. dt is seconds since the last one."""
        self.clamped_joints = set()
        max_step = self.speed_cap * max(0.0, float(dt))
        out = {}
        for joint in poses.JOINTS:
            want = float(requested[joint])
            lo, hi = self.config.soft_limits(joint)

            # An undriven joint has an unknown direction sign, so moving it
            # could drive it into its own end-stop. Pin it to center until M1
            # measures the sign and flips `driven` in config/joints.toml.
            if not self.config.is_driven(joint) and not self.drive_undriven:
                out[joint] = 90.0
                if abs(want - 90.0) > 1e-6:
                    self.clamped_joints.add(joint)
                continue

            if want < lo or want > hi:
                self.clamped_joints.add(joint)
                want = max(lo, min(hi, want))

            prev = self.last[joint]
            step = want - prev
            if abs(step) > max_step:
                step = max_step if step > 0 else -max_step
                self.clamped_joints.add(joint)
            out[joint] = prev + step

        self.last = out
        return dict(out)
