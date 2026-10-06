"""The dome: a reachable sky of look directions, with TALL at the top.

Shaurya's framing, and it is a better frame than raw joint angles. The camera
can only point into part of a sphere, so give that part a coordinate system and
anchor it to a pose he taught by hand rather than to a servo count.

    elevation   angular distance BELOW the pole. 0 at TALL.
    azimuth     rotation around the pole's axis. 0 at base_yaw 90.

TALL IS THE TOP BECAUSE THE ARM CANNOT LOOK HIGHER. Its wrist is 176.0 and the
wrist's soft limit is 176.0, so the pole sits on the edge of reachability, not
in the middle of it. Everything the camera can see lies at or below TALL. That
makes "top of the dome" a physical fact about this arm rather than a label.

WHY ELEVATION IS A SUM. shoulder, elbow and wrist are all pitch joints in one
plane, so they rotate the camera about the SAME axis, and orientation adds
along a serial chain. Elevation is therefore a signed sum of the three, and
each gain should be very close to +-1. The wrist's measured value is 31/32.2 =
0.96, which agrees. base_yaw's 52/83.9 = 0.62 does not, and that is the same
anomaly depth.py reaches from the other direction -- a yaw joint should also
turn the view 1:1.

THE SIGNS WERE NOT MEASURED, THEY WERE INFERRED, and then checked. Of the four
sign combinations, exactly one puts TALL at the top and REST at the bottom:
shoulder -1, elbow -1, wrist +1. It also places the three desk-facing poses
within 4 degrees of each other --

    ATTENTIVE -39.1   OBSERVE -40.7   HOME -43.1

-- which is the real evidence. Those three were taught by hand on different
days and the model independently says they aim at the same place. They are
still estimates until `learn()` has seen the arm move; `unknown()` says which.

WHERE THIS MODEL STOPS BEING TRUE. REST comes out at -269.6 degrees, which is
not a direction on any dome. At that shape the arm is folded back on itself and
a linear sum of orientations no longer describes where the camera looks. So the
dome is valid near the working poses and refuses beyond MAX_ELEVATION rather
than returning a number that is merely arithmetic.
"""
import math

# Which way each pitch joint drives elevation. Inferred, then checked against
# the taught poses; see the module docstring. Replaced by learn().
PITCH_SIGNS = {"shoulder": -1.0, "elbow": -1.0, "wrist": +1.0}

# Degrees of look direction per degree of joint. Orientation adds along a
# serial chain, so +-1 is the physical expectation, and the measured wrist
# gain of 0.96 is consistent with it.
DEFAULT_GAINS = {"shoulder": 1.0, "elbow": 1.0, "wrist": 0.96, "base_yaw": 0.62}

# Gains nobody has measured on the hardware. Listed rather than hidden,
# because a guess that behaves like a measurement is how the old 29.0
# deg-per-frame survived for weeks.
MEASURED = {"wrist", "base_yaw"}

# Beyond this far below the pole the linear model is not trusted. REST sits at
# -269, which is arithmetic rather than a direction.
MAX_ELEVATION = 100.0

POLE = "TALL"


class OffDome(Exception):
    """A direction this frame cannot describe or the arm cannot reach."""


class Dome:
    def __init__(self, poses, pole=POLE, gains=None, signs=None, limits=None):
        if pole not in poses:
            raise OffDome(f"the pole pose {pole!r} is not taught; have "
                          f"{sorted(poses)}")
        self.poses = {k: dict(v) for k, v in poses.items()}
        self.pole = pole
        self.gains = dict(DEFAULT_GAINS, **(gains or {}))
        self.signs = dict(PITCH_SIGNS, **(signs or {}))
        self.limits = dict(limits or {})
        self.learned = set()
        self.fit = {}

    # --- the frame -------------------------------------------------------
    def _pitch(self, joints):
        """Signed sum of the pitch joints, in degrees of camera elevation."""
        return sum(self.signs[j] * self.gains[j] * float(joints[j])
                   for j in PITCH_SIGNS)

    def elevation_of(self, joints):
        """Degrees below the pole. 0 at TALL, negative is impossible."""
        return self._pitch(self.poses[self.pole]) - self._pitch(joints)

    def azimuth_of(self, joints):
        ref = self.poses[self.pole]["base_yaw"]
        return (float(joints["base_yaw"]) - ref) * self.gains["base_yaw"]

    def direction_of(self, joints):
        """(azimuth, elevation) on the dome. Raises off it."""
        el = self.elevation_of(joints)
        if el < -1e-6:
            raise OffDome(
                f"elevation {el:.1f} is above the pole, which the arm cannot "
                f"reach -- the pole is at the wrist's limit")
        if el > MAX_ELEVATION:
            raise OffDome(
                f"elevation {el:.1f} is past {MAX_ELEVATION}; at that shape "
                f"the arm is folded and a linear orientation sum no longer "
                f"says where the camera looks")
        return self.azimuth_of(joints), el

    def unit(self, joints):
        """A unit vector, pole along +z. For angles between directions.

        Spherical geometry does not let azimuth and elevation be compared by
        subtracting them: two directions 40 degrees below the pole and 30
        degrees apart in azimuth are much closer together than 30 degrees.
        """
        az, el = self.direction_of(joints)
        a, e = math.radians(az), math.radians(el)
        return (math.sin(e) * math.cos(a), math.sin(e) * math.sin(a),
                math.cos(e))

    def angle_between(self, j1, j2):
        """True angle between two look directions, in degrees."""
        u, v = self.unit(j1), self.unit(j2)
        return math.degrees(math.acos(
            max(-1.0, min(1.0, sum(a * b for a, b in zip(u, v))))))

    # --- inverse ---------------------------------------------------------
    def _clamp(self, joint, deg):
        lo, hi = self.limits.get(joint, (-math.inf, math.inf))
        return max(lo, min(hi, deg))

    def joints_for(self, azimuth, elevation, prefer=None, tol=0.05):
        """Joint angles that look at a dome direction.

        Three pitch joints serve one degree of elevation, so the arm is
        REDUNDANT and the direction has infinitely many solutions. That is
        useful rather than awkward: the one closest to a pose he taught is
        chosen, so the arm holds postures he picked by hand instead of
        arriving at the same place in some contorted shape of its own.
        """
        if elevation < -1e-6 or elevation > MAX_ELEVATION:
            raise OffDome(f"elevation {elevation:.1f} is not on this dome "
                          f"(0 at the pole, {MAX_ELEVATION} at the edge)")
        prefer = dict(self.poses.get(prefer or self.pole, {})
                      if isinstance(prefer, str) else (prefer or {}))
        if not prefer:
            prefer = dict(self.poses[self.pole])
        out = dict(prefer)

        g = self.gains["base_yaw"]
        if abs(g) < 1e-9:
            raise OffDome("the base_yaw gain is zero; azimuth is unreachable")
        raw_yaw = self.poses[self.pole]["base_yaw"] + azimuth / g
        out["base_yaw"] = self._clamp("base_yaw", raw_yaw)
        # Clamping would hand back a pose that looks somewhere else and claims
        # to be the answer -- the exact failure roommap.look_at refuses. A
        # ring that crosses the limit should lose that arc, not bend it.
        if abs(out["base_yaw"] - raw_yaw) > 1e-6:
            lo, hi = self.limits.get("base_yaw", (-math.inf, math.inf))
            raise OffDome(
                f"azimuth {azimuth:.1f} needs base_yaw {raw_yaw:.1f}, outside "
                f"{lo:.0f}..{hi:.0f}; that part of the dome is behind the arm")

        # Minimum-norm move along the elevation gradient, then clamp and
        # redistribute what the limits refused onto the joints still free.
        want = self.elevation_of(prefer) - elevation      # pitch to add
        free = list(PITCH_SIGNS)
        for _ in range(len(free) + 1):
            grad = {j: self.signs[j] * self.gains[j] for j in free}
            norm = sum(v * v for v in grad.values())
            if norm < 1e-12:
                break
            step = want / norm
            spill, moved = 0.0, False
            for j in list(free):
                raw = out[j] + step * grad[j]
                got = self._clamp(j, raw)
                if abs(got - raw) > 1e-9:
                    free.remove(j)
                    spill += (raw - got) * grad[j]
                    moved = True
                out[j] = got
            want = spill
            if abs(want) < 1e-9 or not free or not moved:
                break
        got = self.elevation_of(out)
        if abs(got - elevation) > max(tol, 1e-6):
            raise OffDome(
                f"the closest reachable elevation to {elevation:.1f} is "
                f"{got:.1f}; the pitch joints hit their limits")
        return out

    def look_at(self, joints_seen, cx, cy, frame=(1280, 720),
                yaw_deg_per_frame=83.9, wrist_deg_per_frame=32.2,
                prefer=None):
        """Joints that centre something currently off-centre in the frame.

        The pixel offset is converted with the same two measured constants the
        tracking loop uses, then expressed on the dome, so the answer survives
        a change of shape in a way a raw joint delta does not.
        """
        w, h = frame
        az, el = self.direction_of(joints_seen)
        az += ((float(cx) - w / 2.0) / w) * yaw_deg_per_frame \
            * self.gains["base_yaw"]
        el += ((float(cy) - h / 2.0) / h) * wrist_deg_per_frame \
            * self.gains["wrist"]
        return self.joints_for(az, el, prefer=prefer or joints_seen)

    # --- learning from the robot's own motion ----------------------------
    def learn(self, samples, frame=(1280, 720), fov=(52.0, 31.0),
              min_samples=3):
        """Fit the gains from the arm watching a fixed landmark while it moves.

        One sample is (joints_before, joints_after, dx_px, dy_px) for the SAME
        landmark. Moving a joint by a known amount and seeing how far the
        landmark slid is a direct measurement of that joint's effect on the
        look direction -- the method that measured the wrist, generalised to
        every joint and solved together.

        This is the part that makes the dome the robot's own: the frame starts
        from poses he taught and then refines itself from motion, rather than
        waiting on anyone to measure four more constants by hand.

        Returns the gains it was able to fit and leaves the rest alone.
        """
        w, h = frame
        fov_x, fov_y = fov
        rows_el, rhs_el, rows_az, rhs_az = [], [], [], []
        for before, after, dx, dy in samples:
            d = {j: float(after[j]) - float(before[j])
                 for j in ("base_yaw",) + tuple(PITCH_SIGNS)}
            # A landmark sliding dx px means the view turned the other way.
            rows_az.append(d["base_yaw"])
            rhs_az.append(-(dx / w) * fov_x)
            rows_el.append([self.signs[j] * d[j] for j in PITCH_SIGNS])
            rhs_el.append((dy / h) * fov_y)

        got, self.fit = {}, {}
        used_az = [(a, b) for a, b in zip(rows_az, rhs_az) if abs(a) > 1e-6]
        if len(used_az) >= min_samples:
            num = sum(a * b for a, b in used_az)
            den = sum(a * a for a, _ in used_az)
            if den > 1e-9:
                g = num / den
                got["base_yaw"] = g
                self.fit["base_yaw"] = self._quality(
                    [[a] for a, _ in used_az], [b for _, b in used_az], [g])
        # Only rows where a pitch joint actually moved. A base_yaw-only sample
        # has an all-zero pitch row, so it can never be explained and only
        # drags the residual around -- and on real hardware its dy is not even
        # zero. The same mistake on the azimuth side was the actual cause of
        # base_yaw's 3x spread: the guard counted all eight samples while the
        # fit used the two that had moved it.
        used_el = [(row, y) for row, y in zip(rows_el, rhs_el)
                   if any(abs(v) > 1e-6 for v in row)]
        if len(used_el) >= min_samples:
            rows_el = [r for r, _ in used_el]
            rhs_el = [y for _, y in used_el]
            pitch = self._fit_pitch(rows_el, rhs_el)
            got.update(pitch)
            if pitch:
                self.fit["pitch"] = self._quality(
                    rows_el, rhs_el, [pitch[j] for j in PITCH_SIGNS])
        for k, v in got.items():
            self.gains[k] = v
            self.learned.add(k)
        return got

    @staticmethod
    def _quality(rows, rhs, coeffs):
        """How much of the signal the fit actually explains.

        Reported because a gain without it is indistinguishable from noise. A
        two-point fit always passes through both points and so always looks
        perfect, which is exactly how base_yaw came back as 0.21, 0.24, 0.51
        and 0.68 from the same arm -- a 3x spread read out as four
        measurements.
        """
        pred = [sum(c * x for c, x in zip(coeffs, row)) for row in rows]
        resid = [y - p for y, p in zip(rhs, pred)]
        n = len(rhs)
        mean = sum(rhs) / n
        ss = sum((y - mean) ** 2 for y in rhs)
        rss = sum(r * r for r in resid)
        return {"n": n,
                "rms_deg": round((rss / n) ** 0.5, 3),
                "r2": round(1.0 - rss / ss, 4) if ss > 1e-12 else None,
                "trustworthy": bool(n >= 4 and ss > 1e-12
                                    and (1.0 - rss / ss) > 0.9)}

    def _fit_pitch(self, rows, rhs):
        """Least squares for the three pitch gains. Normal equations by hand.

        Only solved when the samples actually separate the joints: if every
        sample moved shoulder and elbow together, their individual gains are
        not identifiable and fitting them returns an arbitrary split of one
        real number. Refusing is the honest outcome.
        """
        names = list(PITCH_SIGNS)
        n = len(names)
        A = [[sum(r[i] * r[j] for r in rows) for j in range(n)]
             for i in range(n)]
        b = [sum(r[i] * y for r, y in zip(rows, rhs)) for i in range(n)]
        # Gaussian elimination with partial pivoting, plus a singularity check.
        M = [row[:] + [b[i]] for i, row in enumerate(A)]
        for c in range(n):
            p = max(range(c, n), key=lambda r: abs(M[r][c]))
            if abs(M[p][c]) < 1e-9:
                return {}            # not identifiable from these samples
            M[c], M[p] = M[p], M[c]
            for r in range(c + 1, n):
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
        x = [0.0] * n
        for c in reversed(range(n)):
            x[c] = (M[c][n] - sum(M[c][k] * x[k]
                                  for k in range(c + 1, n))) / M[c][c]
        return dict(zip(names, x))

    def unknown(self):
        """Gains and signs still taken on faith, with why it matters."""
        out = {}
        for j in list(PITCH_SIGNS) + ["base_yaw"]:
            if j in self.learned:
                continue
            if j in MEASURED:
                out[j] = "measured on hardware, not re-fitted here"
            else:
                out[j] = ("never measured: +-1 assumed because orientation "
                          "adds along a serial chain")
        return out

    def azimuth_limits(self):
        """The azimuth the arm can actually reach, in degrees from the pole.

        SMALLER THAN IT SOUNDS, and worth knowing before planning any sweep.
        base_yaw spans 140 degrees of command, but the look direction turns by
        the gain times that -- 0.62 as measured -- so the dome is about 87
        degrees wide, roughly +-43. It is a patch of sky, not a hemisphere.
        Covering a whole room needs the base physically repositioned; no
        sequence of joint moves will do it.
        """
        lo, hi = self.limits.get("base_yaw", (-math.inf, math.inf))
        ref = self.poses[self.pole]["base_yaw"]
        g = self.gains["base_yaw"]
        a, b = (lo - ref) * g, (hi - ref) * g
        return (min(a, b), max(a, b))

    def survey_ring(self, elevation, count, prefer=None, margin=0.5):
        """Poses spanning one ring of the dome at a constant elevation.

        A ring keeps the camera's tilt fixed while sweeping, so everything in
        it is seen from the same height and the row can be compared with
        itself. It spans the REACHABLE azimuth rather than a full circle,
        because most of a circle is behind the arm and asking for it would
        return a handful of points with silent gaps between them.
        """
        lo, hi = self.azimuth_limits()
        lo, hi = lo + margin, hi - margin
        n = max(1, int(count))
        out = []
        for i in range(n):
            az = lo if n == 1 else lo + (hi - lo) * i / (n - 1)
            try:
                out.append((round(az, 1), self.joints_for(
                    az, elevation, prefer=prefer)))
            except OffDome:
                continue
        return out
