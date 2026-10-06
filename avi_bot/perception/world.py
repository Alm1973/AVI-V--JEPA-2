"""Pixel + pose -> a direction in the WORLD: azimuth, and elevation above level.

The room map stores directions as joint angles, (base_yaw, wrist), and turns
a pixel offset into a joint offset with one constant per axis. That is a
flat-earth approximation of a sphere. It holds near the tilt the constant was
fitted at and drifts away from it: looking down at 60 degrees, one degree of
base turn moves the picture by only cos(60) = half a degree, so the same
pixel offset means TWICE the azimuth it means at level. Objects seen from two
tilts land in two places, and the map grew a size-scaled merge threshold to
paper over exactly that.

This module does the geometry properly, now that [horizon] says where level
is. World frame: z along the base axis (up), x toward base_yaw = centre,
azimuth increasing to the camera's RIGHT (the direction +base_yaw turns it,
measured: a +8 deg turn slides the picture left). Elevation is above the
plane perpendicular to the base axis.

The camera's orientation on TALL's shape is two numbers:
    azimuth    = yaw_gain  * (base_yaw - centre)
    pitch      = wrist_gain * (wrist - level_wrist)
and a pixel's ray is the pinhole ray rotated by them. Pitch beyond -90 (wrist
below ~70) is looking down past the nadir; the rotation handles it without
special cases, which is why this works with matrices and not with angles.

What is left out, deliberately: the lens is not on the base axis (the
[horizon] parallax), so a NEAR object's direction shifts by up to
offset/distance x the turn between views. Correcting that needs one length
from a ruler. Directions here are from the camera, at its current place.
"""
import math

import numpy as np


class View:
    def __init__(self, level_wrist, wrist_gain=1.0, yaw_gain=1.0,
                 fov_deg=48.6, width=1280, height=720, centre_yaw=90.0):
        self.level_wrist = float(level_wrist)
        self.wrist_gain = float(wrist_gain)
        self.yaw_gain = float(yaw_gain)
        self.width, self.height = int(width), int(height)
        self.centre_yaw = float(centre_yaw)
        self.fov_deg = float(fov_deg)
        # fov_deg is in the LINEAR convention the [camera]/[horizon] numbers
        # use (frame width / pixels-per-degree at the centre). The pinhole
        # focal length that matches it at the centre:
        self.focal = (self.width / float(fov_deg)) * 180.0 / math.pi

    @classmethod
    def from_config(cls, config, width=1280, height=720):
        """From [horizon]; None if level has not been measured."""
        h = dict(getattr(config, "raw", {}).get("horizon", {}))
        if not h.get("measured"):
            return None
        return cls(h["level_wrist"], h.get("wrist_gain", 1.0),
                   h.get("yaw_gain", 1.0), h.get("fov_deg", 48.6),
                   width, height)

    def to_dict(self):
        return {"level_wrist": self.level_wrist,
                "wrist_gain": self.wrist_gain, "yaw_gain": self.yaw_gain,
                "fov_deg": self.fov_deg, "width": self.width,
                "height": self.height, "centre_yaw": self.centre_yaw}

    # --- camera orientation -------------------------------------------
    def camera(self, yaw, wrist):
        """(azimuth, pitch) of the optical axis, degrees. Pitch is NOT
        wrapped: -120 means looking down and back past the nadir."""
        return (self.yaw_gain * (float(yaw) - self.centre_yaw),
                self.wrist_gain * (float(wrist) - self.level_wrist))

    def _axes(self, yaw, wrist):
        az, pitch = (math.radians(v) for v in self.camera(yaw, wrist))
        ca, sa, ce, se = math.cos(az), math.sin(az), math.cos(pitch), \
            math.sin(pitch)
        fwd = np.array([ce * ca, ce * sa, se])
        right = np.array([-sa, ca, 0.0])
        up = np.array([-se * ca, -se * sa, ce])
        return fwd, right, up

    # --- forward and inverse ------------------------------------------
    def ray(self, px, py, yaw, wrist):
        """Unit vector, in the world, through pixel (px, py)."""
        fwd, right, up = self._axes(yaw, wrist)
        x = (float(px) - self.width / 2.0) / self.focal
        y = (float(py) - self.height / 2.0) / self.focal
        d = fwd + x * right - y * up
        return d / np.linalg.norm(d)

    def direction(self, px, py, yaw, wrist):
        """(azimuth, elevation) in degrees of the ray through a pixel."""
        return to_angles(self.ray(px, py, yaw, wrist))

    def pixel(self, direction, yaw, wrist):
        """Where a world direction lands in the picture, or None if it is
        behind the camera. `direction` is (azimuth, elevation) or a vector."""
        d = (unit(*direction) if len(direction) == 2
             else np.asarray(direction, dtype=float))
        fwd, right, up = self._axes(yaw, wrist)
        z = float(d @ fwd)
        if z <= 1e-9:
            return None
        return (self.width / 2.0 + self.focal * float(d @ right) / z,
                self.height / 2.0 - self.focal * float(d @ up) / z)

    def aim(self, azimuth, elevation):
        """(base_yaw, wrist) that puts a world direction at frame centre.
        Exact: on this shape the optical axis IS (azimuth, pitch)."""
        return (self.centre_yaw + float(azimuth) / self.yaw_gain,
                self.level_wrist + float(elevation) / self.wrist_gain)


def unit(azimuth, elevation):
    a, e = math.radians(azimuth), math.radians(elevation)
    return np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a),
                     math.sin(e)])


def to_angles(v):
    v = np.asarray(v, dtype=float)
    v = v / np.linalg.norm(v)
    return (math.degrees(math.atan2(v[1], v[0])),
            math.degrees(math.asin(max(-1.0, min(1.0, v[2])))))


def angle_between(a, b):
    """Degrees between two directions, each (az, el) or a vector."""
    va = unit(*a) if len(a) == 2 else np.asarray(a, dtype=float)
    vb = unit(*b) if len(b) == 2 else np.asarray(b, dtype=float)
    c = float(va @ vb) / (np.linalg.norm(va) * np.linalg.norm(vb))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def similarity(pts_a, pts_b):
    """Least-squares roll/scale/slide taking pts_a to pts_b (Umeyama, 2D).
    Numpy only, so the model can be checked against /probe_horizon's
    measured roll and slide without OpenCV."""
    a, b = np.asarray(pts_a, float), np.asarray(pts_b, float)
    ma, mb = a.mean(0), b.mean(0)
    A, B = a - ma, b - mb
    U, S, Vt = np.linalg.svd(B.T @ A)
    D = np.diag([1.0, np.sign(np.linalg.det(U @ Vt))])
    R = U @ D @ Vt
    s = float(np.trace(np.diag(S) @ D) / (A ** 2).sum())
    t = mb - s * R @ ma
    return np.column_stack([s * R, t])
