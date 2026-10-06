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

from . import optics

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

    # --- the pole's own shape: a 2-DOF dome where the model is EXACT -----
    #
    # Shaurya's instruction, and it repairs the hole in everything above. The
    # three-pitch-joint model predicts where the camera POINTS and ignores
    # where it IS -- and on a serial arm shoulder and elbow move the lens a
    # long way. Measured: OBSERVE sits between HOME and ATTENTIVE in pitch sum
    # and sees something completely different (nothing at all, in fact), and
    # once the gains were measured the pose ordering stopped matching the names
    # Shaurya gave them.
    #
    # Holding shoulder and elbow at the POLE's values removes the problem
    # rather than modelling it. The camera then sits in one fixed place and
    # only turns, which is the one regime where elevation really is linear in
    # the joint. base_yaw pans, the wrist tilts, and nothing translates.
    #
    # The wrist alone spans the whole dome from the pole: 176 down to 20 is 156
    # degrees of travel, and at the measured gain that is nearly 200 degrees of
    # view. There was never a need to involve shoulder or elbow in looking
    # around.

    def pole_shape(self):
        """Shoulder and elbow as the pole holds them. Held for every pose."""
        p = self.poses[self.pole]
        return {"shoulder": float(p["shoulder"]), "elbow": float(p["elbow"])}

    def pose_at(self, azimuth, elevation):
        """The pose that looks at a dome direction, from the pole's shape.

        Exact rather than approximate: only base_yaw and wrist move, so the
        camera rotates in place and no translation is unaccounted for.
        """
        pole = self.poses[self.pole]
        gw, gy = self.gains["wrist"], self.gains["base_yaw"]
        if abs(gw) < 1e-9 or abs(gy) < 1e-9:
            raise OffDome("a zero gain makes the dome unreachable")
        out = dict(self.pole_shape())
        out["wrist"] = float(pole["wrist"]) - float(elevation) / gw
        out["base_yaw"] = float(pole["base_yaw"]) + float(azimuth) / gy
        for j, deg in (("wrist", out["wrist"]), ("base_yaw", out["base_yaw"])):
            lo, hi = self.limits.get(j, (-math.inf, math.inf))
            if not (lo - 1e-6 <= deg <= hi + 1e-6):
                raise OffDome(
                    f"az {azimuth:.1f} el {elevation:.1f} needs {j} "
                    f"{deg:.1f}, outside {lo:.0f}..{hi:.0f}")
        return out

    def direction_at(self, joints):
        """(azimuth, elevation) for a pose on the pole's shape. The inverse."""
        pole = self.poses[self.pole]
        el = (float(pole["wrist"]) - float(joints["wrist"])) \
            * self.gains["wrist"]
        az = (float(joints["base_yaw"]) - float(pole["base_yaw"])) \
            * self.gains["base_yaw"]
        return az, el

    def elevation_span(self):
        """How far down the dome the wrist alone can look."""
        lo, hi = self.limits.get("wrist", (-math.inf, math.inf))
        pole_wrist = float(self.poses[self.pole]["wrist"])
        gw = self.gains["wrist"]
        return (round((pole_wrist - hi) * gw, 1),
                round((pole_wrist - lo) * gw, 1))

    def ladder(self, step=12.0, azimuth=0.0, limit=None, start=0.0):
        """Poses stepping DOWN the dome from the pole. His "work your way down".

        Stops when the wrist runs out, so the last rung is the lowest the arm
        can actually look rather than the lowest that was asked for.

        `start` skips the top of the dome. Measured 2026-10-06: every stop at
        elevation 0 -- the pole's own tilt -- kept nothing across a whole
        survey, because it aims at wall and ceiling. It is also where the
        detector's false "person" boxes live, 3 to 6 per stop, and a check at
        TALL showed one of them to be a 347x109 strip at cy 55, the very top
        edge of the frame. Furniture, not a person. Since the survey skips
        person labels, that row costs stops and returns nothing.
        """
        lo_el, hi_el = self.elevation_span()
        far = hi_el if limit is None else min(hi_el, float(limit))
        out, el = [], max(float(start), lo_el)
        while el <= far + 1e-9:
            try:
                out.append((round(el, 1), self.pose_at(azimuth, el)))
            except OffDome:
                break
            el += float(step)
        return out

    def grid(self, el_step=12.0, az_step=15.0, limit=None, start=0.0):
        """The whole reachable dome, as rungs of a ladder across azimuth.

        Rows of constant elevation: the camera's tilt is fixed along a row, so
        everything in it is seen from the same height and can be compared with
        itself.
        """
        az_lo, az_hi = self.azimuth_limits()
        out = []
        for el, _ in self.ladder(step=el_step, limit=limit, start=start):
            az = az_lo + 0.5
            while az <= az_hi - 0.5 + 1e-9:
                try:
                    out.append((round(az, 1), round(el, 1),
                                self.pose_at(az, el)))
                except OffDome:
                    pass
                az += float(az_step)
        return out

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
        # LINEAR on purpose: yaw_deg_per_frame and wrist_deg_per_frame were
        # fitted as straight lines through pixel offset, so those numbers mean
        # linear. A pinhole conversion here would be better physics applied to
        # constants that do not match it. See optics.py.
        az += optics.angle_linear(float(cx) - w / 2.0, w, yaw_deg_per_frame) \
            * self.gains["base_yaw"]
        el += optics.angle_linear(float(cy) - h / 2.0, h, wrist_deg_per_frame) \
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
        # Kept per sample so a bad fit can be DIAGNOSED rather than only
        # flagged. Three different causes look identical in an r2 alone:
        # sensor noise scatters residuals randomly, a nonlinearity bends them
        # with magnitude, and BACKLASH splits them by direction of travel --
        # and the base joint carries the most load, so it is the likeliest to
        # have it. Only the third is fixable by approaching from one side.
        self.residuals = []
        for before, after, dx, dy in samples:
            d = {j: float(after[j]) - float(before[j])
                 for j in ("base_yaw",) + tuple(PITCH_SIGNS)}
            # A landmark sliding dx px means the view turned the other way.
            rows_az.append(d["base_yaw"])
            rhs_az.append(-(dx / w) * fov_x)
            rows_el.append([self.signs[j] * d[j] for j in PITCH_SIGNS])
            rhs_el.append((dy / h) * fov_y)
            moved = [j for j in d if abs(d[j]) > 1e-6]
            self.residuals.append({
                "joint": moved[0] if len(moved) == 1 else "+".join(moved),
                "delta": round(sum(d[j] for j in moved), 3) if moved else 0.0,
                "dx": round(dx, 1), "dy": round(dy, 1)})

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
        self._attach_residuals(fov, frame)
        return got

    def _fit_pitch(self, rows, rhs):
        """Least squares for the three pitch gains, via _rss's solver.

        Only solved when the samples actually separate the joints: if every
        sample moved shoulder and elbow together, their individual gains are
        not identifiable and fitting them returns an arbitrary split of one
        real number. Refusing is the honest outcome.
        """
        names = list(PITCH_SIGNS)
        coef = self._solve(rows, rhs)
        return {} if coef is None else dict(zip(names, coef))

    @staticmethod
    def _solve(xs, ys):
        """Coefficients of y = sum(b_k * x_k), or None if not identifiable."""
        k = len(xs[0])
        A = [[sum(r[i] * r[j] for r in xs) for j in range(k)] for i in range(k)]
        b = [sum(r[i] * y for r, y in zip(xs, ys)) for i in range(k)]
        M = [A[i][:] + [b[i]] for i in range(k)]
        for c in range(k):
            pv = max(range(c, k), key=lambda r: abs(M[r][c]))
            if abs(M[pv][c]) < 1e-9:
                return None
            M[c], M[pv] = M[pv], M[c]
            for r in range(c + 1, k):
                f = M[r][c] / M[c][c]
                for j in range(c, k + 1):
                    M[r][j] -= f * M[c][j]
        coef = [0.0] * k
        for c in reversed(range(k)):
            coef[c] = (M[c][k] - sum(M[c][j] * coef[j]
                                     for j in range(c + 1, k))) / M[c][c]
        return coef

    @staticmethod
    def _quality(rows, rhs, coeffs):
        """How much of the signal the fit actually explains.

        Reported because a gain without it is indistinguishable from noise. A
        two-point fit always passes through both points and so always looks
        perfect, which is how base_yaw came back as 0.21, 0.24, 0.51 and 0.68
        from the same arm -- a 3x spread read out as four measurements.
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

    def _attach_residuals(self, fov, frame):
        """Predicted-minus-observed per sample, in degrees of view."""
        fov_x, fov_y = fov
        w, h = frame
        for r in self.residuals:
            j, delta = r["joint"], r["delta"]
            if j == "base_yaw":
                want = -(self.gains["base_yaw"] * delta / fov_x) * w
                r["predicted_px"] = round(want, 1)
                r["resid_px"] = round(r["dx"] - want, 1)
            elif j in PITCH_SIGNS:
                want = (self.signs[j] * self.gains[j] * delta / fov_y) * h
                r["predicted_px"] = round(want, 1)
                r["resid_px"] = round(r["dy"] - want, 1)

    @classmethod
    def _rss(cls, xs, ys):
        """Residual sum of squares for the best y = sum(b_k * x_k)."""
        coef = cls._solve(xs, ys)
        if coef is None:
            return None
        return sum((y - sum(c * x for c, x in zip(coef, r))) ** 2
                   for r, y in zip(xs, ys))

    def diagnose(self, joint):
        """Why a joint's fit is poor: noise, nonlinearity, or backlash.

        By NESTED MODEL COMPARISON, not by eyeballing residuals. The first
        version grouped residuals by sign, and it failed its own test for a
        reason worth keeping: fitting y = b*s with no extra term lets a
        direction-dependent offset leak straight into b, which flattens the
        very grouping being looked for. Backlash hides inside the gain.

        So three models are fitted and compared by how much variance each
        extra term explains:

            linear      y = b*s
            backlash    y = b*s + c*sign(s)      a fixed offset by direction
            nonlinear   y = b*s + d*s*|s|        an error growing with size

        Backlash is the one worth separating. It is not a modelling error --
        the joint genuinely rests in a different place depending on which way
        it arrived -- and the cure is to approach every target from one side,
        not to fit a better curve. The base joint carries the most load, so it
        is the likeliest to have it.
        """
        rows = [r for r in self.residuals
                if r["joint"] == joint and "predicted_px" in r]
        if len(rows) < 4:
            return {"verdict": "too few samples to diagnose", "n": len(rows)}
        axis = "dx" if joint == "base_yaw" else "dy"
        ss = [r["delta"] for r in rows]
        ys = [r[axis] for r in rows]
        if not (any(x > 0 for x in ss) and any(x < 0 for x in ss)):
            return {"verdict": "only one direction of travel sampled",
                    "n": len(rows)}
        mean = sum(ys) / len(ys)
        total = sum((y - mean) ** 2 for y in ys)
        lin = self._rss([[x] for x in ss], ys)
        back = self._rss([[x, 1.0 if x > 0 else -1.0] for x in ss], ys)
        nl = self._rss([[x, x * abs(x)] for x in ss], ys)
        if lin is None or total <= 1e-12:
            return {"verdict": "degenerate fit", "n": len(rows)}

        # Normalised by what the LINEAR model failed to explain, not by total
        # variance. Over +-8 degrees the signal spans ~122 px, so a real 25 px
        # backlash is under 3% of total variance and reads as "clean" -- while
        # being 100% of the part a single gain cannot account for, which is
        # the question actually being asked.
        def share(rss):
            return 0.0 if rss is None or lin <= 1e-12 else max(
                0.0, min(1.0, (lin - rss) / lin))

        rms_px = (lin / len(rows)) ** 0.5
        s_back, s_nl = share(back), share(nl)
        out = {"n": len(rows), "rms_px": round(rms_px, 2),
               "unexplained_by_linear": round(lin / total, 4),
               "backlash_explains": round(s_back, 3),
               "nonlinearity_explains": round(s_nl, 3)}
        # Clean is an ABSOLUTE judgement. A few pixels of residual is clean
        # however it is distributed, and box-centre jitter alone is a few px.
        if rms_px < 4.0:
            out["verdict"] = "CLEAN: a single gain fits this joint"
        elif s_back >= s_nl and s_back > 0.5:
            out["verdict"] = ("BACKLASH: a direction-dependent offset explains "
                              f"{s_back:.0%} of what one gain cannot. The joint "
                              "rests differently depending on which way it "
                              "arrived -- approach every target from one side.")
        elif s_nl > 0.5:
            out["verdict"] = ("NONLINEAR: the error grows with the size of the "
                              f"move, explaining {s_nl:.0%} of the shortfall. "
                              "One gain cannot cover the whole range.")
        else:
            out["verdict"] = (f"NOISE: {rms_px:.0f} px of scatter that neither "
                              "direction nor size explains. Average more "
                              "samples.")
        return out

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
