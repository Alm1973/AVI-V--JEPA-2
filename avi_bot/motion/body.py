"""AVI's body in 3D: joint angles -> where each part is and where it looks.

WHY THIS EXISTS (docs/RESEARCH-motion.md). Shaurya asked whether giving JEPA
a 3D model of the robot would give it full control of its own body. What the
research says, and what this module does about it:

  * V-JEPA 2-AC (Meta, 2025) gets motor control from an ACTION-CONDITIONED
    predictor trained on ~62 h of robot video plus the arm's own state. It is
    never handed a mesh: the encoder sees, the predictor learns what an action
    does to what is seen. A 3D model does not go INTO JEPA.
  * Lipson's self-modelling robots (Science Robotics 2022, Nature Machine
    Intelligence 2025) learn a model of their own body from a camera, then
    use it to plan and to notice when a joint is not doing what it should.

So the useful pairing is: a body model PREDICTS what a move should do to the
picture, and JEPA (with ORB) CHECKS it. Before this, the judge could only
predict base_yaw and wrist moves -- a shoulder or elbow move had no expected
direction, so it was never judged at all (verify.dominant). The same model
also says where the lens is, which is what keeps an improvised dance from
driving the camera into the desk, and it is what the dashboard draws.

THE CHAIN, in the arm's vertical plane (r forward, z up from the desk), with
psi measured from straight up, positive leaning forward:

    psi_upper  = g_s * (shoulder - upright)       upper arm
    psi_fore   = psi_upper + g_e * (elbow - elbow_straight)
    psi_camera = psi_fore - g_w * (wrist - wrist_mount)   optical axis
    elevation  = 90 - psi_camera                  above level

The signs are the dome's (perception/dome.py PITCH_SIGNS): higher shoulder
and elbow look lower, higher wrist looks up. Taught data agrees: LOWER
shoulder degrees raise the arm (ATTENTIVE 29.5, REST 150). The base turns the
whole plane: azimuth = yaw_gain * (base_yaw - centre), positive to the right.

WHAT IS MEASURED AND WHAT IS NOT. Measured: the lens at TALL is 25.0 cm out
from the base shaft and 34.4 cm above the desk (ruler, 2026-10-08); level is
wrist 160.3 at TALL, wrist gain 1.03, yaw gain 0.95 ([horizon]). NOT measured:
the three link lengths. They are priors in [body], and elbow_straight and
wrist_mount are then FITTED so that TALL lands exactly on the ruler numbers.
Directions (what judging needs) do not depend on the lengths at all;
positions do, so clearance is used only as an extra veto with a margin, never
as the only fence -- the soft limits and the firmware are still that.
"""
import math

from . import poses

# Priors, used only when config/joints.toml has no [body] section.
DEFAULTS = {
    "measured": False,
    "base_height_m": 0.10,     # desk to the shoulder axle
    "upper_arm_m": 0.15,       # shoulder axle to elbow axle
    "lens_m": 0.06,            # wrist axle to the lens
    "upright": 90.0,           # shoulder logical angle with the arm vertical
    "shoulder_gain": 1.0,
    "elbow_gain": 1.0,
    # Fitted, not configured (see Body.fit):
    #   forearm_m, elbow_straight, wrist_mount
}

# The ruler numbers the fit is anchored to ([horizon] in joints.toml).
TALL = {"base_yaw": 90.0, "shoulder": 78.4, "elbow": 88.0, "wrist": 176.0}

# A part closer to the desk than this is a veto for an improvised move. Large
# on purpose: the lengths are priors, so the model's heights can be off by a
# few centimetres, and a dance has no business near the desk anyway.
MIN_CLEARANCE_M = 0.06

PARTS = ("base", "shoulder", "elbow", "wrist", "lens")


def _dir(psi_deg):
    p = math.radians(psi_deg)
    return math.sin(p), math.cos(p)


class Body:
    def __init__(self, base_height_m, upper_arm_m, forearm_m, lens_m,
                 elbow_straight, wrist_mount, upright=90.0, centre_yaw=90.0,
                 yaw_gain=0.95, shoulder_gain=1.0, elbow_gain=1.0,
                 wrist_gain=1.03, measured=False, reach_judged=False):
        self.base_height_m = float(base_height_m)
        self.upper_arm_m = float(upper_arm_m)
        self.forearm_m = float(forearm_m)
        self.lens_m = float(lens_m)
        self.elbow_straight = float(elbow_straight)
        self.wrist_mount = float(wrist_mount)
        self.upright = float(upright)
        self.centre_yaw = float(centre_yaw)
        self.yaw_gain = float(yaw_gain)
        self.shoulder_gain = float(shoulder_gain)
        self.elbow_gain = float(elbow_gain)
        self.wrist_gain = float(wrist_gain)
        self.measured = bool(measured)
        # Whether shoulder and elbow count when predicting a move's
        # direction for the judge. Off until POST /verify_move with a joint
        # has shown the model's sign right on the robot: a wrong prediction
        # would have AVI apologise for moves that went fine.
        self.reach_judged = bool(reach_judged)

    # --- building it ----------------------------------------------------
    @classmethod
    def fit(cls, lens_r_m, lens_z_m, level_wrist, anchor=None,
            base_height_m=DEFAULTS["base_height_m"],
            upper_arm_m=DEFAULTS["upper_arm_m"], lens_m=DEFAULTS["lens_m"],
            upright=DEFAULTS["upright"], yaw_gain=0.95,
            shoulder_gain=1.0, elbow_gain=1.0, wrist_gain=1.03,
            measured=False, reach_judged=False):
        """The body that puts the lens exactly where the ruler says at
        `anchor` (TALL), looking at the elevation [horizon] measured there.

        Given the shoulder axle (base height + upper arm at the anchor's
        shoulder angle) and the lens with its viewing direction, the wrist
        axle is fixed -- one lens length back along the optical axis -- and
        the forearm is whatever joins the two. That gives the forearm's
        length and the elbow angle at which it would run straight on.
        """
        a = dict(anchor or TALL)
        psi_u = shoulder_gain * (a["shoulder"] - upright)
        elev = wrist_gain * (a["wrist"] - level_wrist)
        psi_c = 90.0 - elev
        ur, uz = _dir(psi_u)
        elbow = (upper_arm_m * ur, base_height_m + upper_arm_m * uz)
        cr, cz = _dir(psi_c)
        wrist = (lens_r_m - lens_m * cr, lens_z_m - lens_m * cz)
        dr, dz = wrist[0] - elbow[0], wrist[1] - elbow[1]
        forearm = math.hypot(dr, dz)
        if forearm < 1e-3:
            raise ValueError("the priors put the wrist on the elbow")
        psi_f = math.degrees(math.atan2(dr, dz))
        elbow_straight = a["elbow"] - (psi_f - psi_u) / elbow_gain
        wrist_mount = a["wrist"] - (psi_f - psi_c) / wrist_gain
        return cls(base_height_m, upper_arm_m, forearm, lens_m,
                   elbow_straight, wrist_mount, upright=upright,
                   yaw_gain=yaw_gain, shoulder_gain=shoulder_gain,
                   elbow_gain=elbow_gain, wrist_gain=wrist_gain,
                   measured=measured, reach_judged=reach_judged)

    @classmethod
    def from_config(cls, config):
        """From [horizon] (the ruler and level) and [body] (the priors).
        None when [horizon] has not been measured: without the anchor there
        is nothing to fit to."""
        raw = getattr(config, "raw", {}) or {}
        h = dict(raw.get("horizon", {}))
        if not h.get("measured") or not h.get("lens_height_m"):
            return None
        b = dict(DEFAULTS, **dict(raw.get("body", {})))
        z = float(h["lens_height_m"])
        r = float(h.get("offset_over_height", 0.0)) * z
        return cls.fit(r, z, float(h["level_wrist"]),
                       base_height_m=float(b["base_height_m"]),
                       upper_arm_m=float(b["upper_arm_m"]),
                       lens_m=float(b["lens_m"]),
                       upright=float(b["upright"]),
                       yaw_gain=float(h.get("yaw_gain", 0.95)),
                       shoulder_gain=float(b["shoulder_gain"]),
                       elbow_gain=float(b["elbow_gain"]),
                       wrist_gain=float(h.get("wrist_gain", 1.03)),
                       measured=bool(b["measured"]),
                       reach_judged=bool(b.get("reach_judged", False)))

    def to_dict(self):
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in vars(self).items()}

    # --- the chain ------------------------------------------------------
    def angles(self, joints):
        """(psi_upper, psi_fore, psi_camera), degrees from straight up,
        positive leaning forward."""
        psi_u = self.shoulder_gain * (float(joints["shoulder"])
                                      - self.upright)
        psi_f = psi_u + self.elbow_gain * (float(joints["elbow"])
                                           - self.elbow_straight)
        psi_c = psi_f - self.wrist_gain * (float(joints["wrist"])
                                           - self.wrist_mount)
        return psi_u, psi_f, psi_c

    def azimuth(self, joints):
        """Degrees, positive to the camera's right."""
        return self.yaw_gain * (float(joints["base_yaw"]) - self.centre_yaw)

    def elevation(self, joints):
        """Where the camera looks, degrees above level, wrapped to
        -180..180 (beyond +-90 it is looking back over the top)."""
        e = 90.0 - self.angles(joints)[2]
        return (e + 180.0) % 360.0 - 180.0

    def points(self, joints):
        """{part: (x, y, z)} in metres. Origin on the desk under the base
        shaft, z up, x ahead at base centre, y to the robot's left."""
        psi_u, psi_f, psi_c = self.angles(joints)
        a = math.radians(self.azimuth(joints))
        fx, fy = math.cos(a), -math.sin(a)   # +azimuth turns right (-y)
        r, z = 0.0, self.base_height_m
        plane = [("base", 0.0, 0.0), ("shoulder", r, z)]
        for name, length, psi in (("elbow", self.upper_arm_m, psi_u),
                                  ("wrist", self.forearm_m, psi_f),
                                  ("lens", self.lens_m, psi_c)):
            dr, dz = _dir(psi)
            r, z = r + length * dr, z + length * dz
            plane.append((name, r, z))
        return {name: (round(pr * fx, 4), round(pr * fy, 4), round(pz, 4))
                for name, pr, pz in plane}

    def look(self, joints):
        """Unit vector of the optical axis, same frame as points()."""
        el = math.radians(self.elevation(joints))
        a = math.radians(self.azimuth(joints))
        return (math.cos(el) * math.cos(a), -math.cos(el) * math.sin(a),
                math.sin(el))

    def clearance(self, joints):
        """Height above the desk of the lowest moving part (elbow, wrist,
        lens), metres."""
        p = self.points(joints)
        return min(p[k][2] for k in ("elbow", "wrist", "lens"))

    def safe(self, joints, margin=MIN_CLEARANCE_M):
        return self.clearance(joints) >= margin

    # --- what a move should do to the picture ----------------------------
    def view_change(self, before, after):
        """(d_azimuth, d_elevation) the camera should turn, degrees; right
        and up positive. From orientation only, so the unmeasured lengths
        do not enter it."""
        dz = self.azimuth(after) - self.azimuth(before)
        de = self.elevation(after) - self.elevation(before)
        de = (de + 180.0) % 360.0 - 180.0
        return dz, de

    def direction(self, before, after, min_deg=2.0, reach=None):
        """The word the move judge should expect ("left"/"right"/"up"/
        "down"), from the larger of the two turns; None below `min_deg`.
        `reach` (default: reach_judged) counts shoulder and elbow."""
        full = {j: float(before[j]) for j in poses.JOINTS if j in before}
        to = dict(full, **{j: float(after[j]) for j in poses.JOINTS
                          if j in after})
        if set(full) != set(poses.JOINTS):
            return None
        if not (self.reach_judged if reach is None else reach):
            to["shoulder"], to["elbow"] = full["shoulder"], full["elbow"]
        dz, de = self.view_change(full, to)
        if max(abs(dz), abs(de)) < min_deg:
            return None
        if abs(dz) >= abs(de):
            return "right" if dz > 0 else "left"
        return "up" if de > 0 else "down"


_BODY = None


def load(config=None):
    """The body from config/joints.toml, cached. None if unmeasured."""
    global _BODY
    if config is not None:
        return Body.from_config(config)
    if _BODY is None:
        _BODY = Body.from_config(poses.load())
    return _BODY


def state(pose):
    """The model at `pose` for the dashboard's 3D view; None without one."""
    model = load()
    if model is None:
        return None
    try:
        return {"points": model.points(pose),
                "look": [round(v, 3) for v in model.look(pose)],
                "elevation": round(model.elevation(pose), 1),
                "measured": model.measured}
    except Exception:
        return None
