"""Where something was last seen. BUILD_SPEC 9.

The acceptance test is "a seeded database gives the right answer and the right
yaw", and the yaw half is what makes this a robot skill rather than a database
query: after answering, AVI should LOOK at where the thing was, which means
turning a stored image position back into a joint angle.

That conversion uses the measured `yaw_deg_per_frame` (83.9, see
config/joints.toml), and it is the one place a wrong constant would be
invisible -- the sentence would still be right while the arm pointed at the
wrong part of the room. That is not hypothetical; it had happened three times
over in this file at once:

  * `find()` read a `cx` column. Production has no such column -- sightings
    store `x, y, w, h` as FRACTIONS of the frame -- so the yaw was always None
    and AVI never once looked at what it found. The test fixture declared its
    own table WITH a cx column, which is exactly why nobody noticed.

  * `centre_yaw` defaulted to 90. The logger only records from OBSERVE, and
    OBSERVE was retaught to base_yaw 60, so every answer would have been 30
    degrees out once the first bug was fixed -- a third of a frame width. A
    default had quietly encoded a pose value that later changed.

  * A stored image position is only a direction if you know where the camera
    was pointing when it was stored. Nothing recorded that. So a sighting now
    yields a direction only when the pose it was taken from is supplied, and
    otherwise says it does not know rather than guessing.

The room map, when it knows the object, is preferred over all of this: it
stores an ABSOLUTE direction accumulated from many viewpoints, so it does not
depend on remembering where the camera was.

Not seen today gets a shake and "Haven't seen it today", NOT a guess from
yesterday. A confident wrong answer about where his keys are costs more than
an admission.
"""
import time

# phone -> cell phone, and so on. The detector's vocabulary is not the
# vocabulary someone speaks in.
SYNONYMS = {
    "phone": "cell phone", "mobile": "cell phone", "cell": "cell phone",
    "glasses": "glasses", "specs": "glasses", "spectacles": "glasses",
    "cup": "mug", "coffee": "mug", "tea": "mug",
    "laptop": "laptop", "computer": "laptop", "macbook": "laptop",
    "keys": "keys", "keyring": "keys",
    "headphones": "headphones", "earphones": "headphones",
    "cans": "headphones", "bottle": "water bottle",
    "water": "water bottle", "charger": "charger", "cable": "charger",
    "wallet": "wallet", "pen": "pen", "pencil": "pen", "book": "book",
    "notebook": "notebook", "remote": "remote",
}


def canonical(name):
    n = " ".join((name or "").split()).lower()
    return SYNONYMS.get(n, n)


class Finder:
    def __init__(self, db, yaw_deg_per_frame=83.9, frame_width=1280,
                 centre_yaw=None, clock=time.time, roommap=None,
                 wrist_deg_per_frame=32.2, frame_height=720,
                 centre_wrist=None):
        self.db = db
        self.yaw_deg_per_frame = float(yaw_deg_per_frame)
        self.wrist_deg_per_frame = float(wrist_deg_per_frame)
        self.frame_width = float(frame_width)
        self.frame_height = float(frame_height)
        # Where the camera was pointing when the logger wrote these rows. None
        # on purpose: not knowing is the honest state, and a guessed centre
        # turns a correct sentence into an arm aimed at the wrong wall.
        self.centre_yaw = None if centre_yaw is None else float(centre_yaw)
        self.centre_wrist = None if centre_wrist is None else float(centre_wrist)
        self.roommap = roommap
        self.clock = clock

    def yaw_for(self, cx):
        """Image x -> the base_yaw that would centre it.

        Raising base_yaw moves a fixed landmark LEFT (measured, r2 = 0.976), so
        something to the RIGHT of centre needs MORE yaw.
        """
        if self.centre_yaw is None:
            return None
        offset = (float(cx) - self.frame_width / 2.0) / self.frame_width
        return self.centre_yaw + offset * self.yaw_deg_per_frame

    def wrist_for(self, cy):
        """Image y -> the wrist angle that would centre it.

        Same convention as RoomMap.direction_of, deliberately: one geometry,
        one place for a sign error to live. test_find_agrees_with_roommap
        pins the two together.
        """
        if self.centre_wrist is None:
            return None
        offset = (float(cy) - self.frame_height / 2.0) / self.frame_height
        return self.centre_wrist + offset * self.wrist_deg_per_frame

    def pixel_centre(self, row):
        """The sighting's centre in PIXELS, from whichever columns exist.

        Production stores x and w as fractions of the frame and x is the LEFT
        edge, so the centre is x + w/2 -- the same expression the zone label
        is derived from. A bare `cx` in pixels is also accepted because the
        older fixtures use one.
        """
        x, w = row.get("x"), row.get("w")
        if x is not None:
            frac_x = float(x) + (float(w) / 2.0 if w is not None else 0.0)
            cx = frac_x * self.frame_width
        elif row.get("cx") is not None:
            cx = float(row["cx"])
        else:
            return None, None
        y, h = row.get("y"), row.get("h")
        if y is not None:
            frac_y = float(y) + (float(h) / 2.0 if h is not None else 0.0)
            cy = frac_y * self.frame_height
        elif row.get("cy") is not None:
            cy = float(row["cy"])
        else:
            cy = None
        return cx, cy

    def pose_of(self, row):
        """The arm's joints when the sighting was taken, or None.

        All four or nothing. The camera rides three links, so a base_yaw and a
        wrist without the shoulder and elbow that held them does not determine
        where the camera was looking -- and a half-known pose that reads as an
        answer is worse than a missing one.
        """
        got = {}
        for j in ("base_yaw", "shoulder", "elbow", "wrist"):
            v = row.get(j)
            if v is None:
                return None
            got[j] = float(v)
        return got

    def from_map(self, name):
        """The map's absolute direction for a label, or None.

        Preferred over a stored image position because it does not depend on
        knowing where the camera was: the map accumulated it from several
        viewpoints and stores the (yaw, wrist) that would centre the thing.
        """
        if self.roommap is None:
            return None
        for label in dict.fromkeys((canonical(name), (name or "").lower())):
            got = self.roommap.where_is(label)
            if got:
                yaw, wrist, weight = got
                near = None
                if hasattr(self.roommap, "nearest_anchor"):
                    near = self.roommap.nearest_anchor(yaw, wrist)[0]
                out = {"base_yaw": yaw, "wrist": wrist, "source": "map",
                       "confident": True, "weight": weight, "near": near,
                       "label": label}
                # The map's (yaw, wrist) are only directions AT THE SHAPE IT
                # WAS BUILT AT. The camera rides three links, so the same yaw
                # and wrist from a different shoulder and elbow point
                # somewhere else -- the bug that made OBSERVE see a wall while
                # sitting between two poses that saw the desk. So the shape
                # travels with the direction, and the caller must set it too.
                sh = getattr(self.roommap, "shoulder", None)
                el = getattr(self.roommap, "elbow", None)
                if sh is not None and el is not None:
                    out["shoulder"], out["elbow"] = float(sh), float(el)
                return out
        return None

    def aim(self, name, within_s=86400):
        """Where to point to see it. None when that cannot be known.

        `confident` is the whole point of the return value: only a direction
        from the map, or one from a sighting whose pose is known, is safe to
        drive the arm with.
        """
        from_map = self.from_map(name)
        if from_map:
            return from_map
        seen = self.last_seen(name, within_s=within_s)
        if seen is None:
            return None
        cx, cy = self.pixel_centre(seen)
        if cx is None:
            return None
        # The row's OWN pose beats the assumed one. A sighting recorded with
        # its angles is self-describing and stays correct when a pose is
        # retaught; falling back on "wherever OBSERVE points now" is what
        # silently broke when OBSERVE moved from 90 to 60.
        pose = self.pose_of(seen)
        centre_yaw = pose["base_yaw"] if pose else self.centre_yaw
        centre_wrist = pose["wrist"] if pose else self.centre_wrist
        if centre_yaw is None:
            # A position in a frame whose pose nobody recorded. Real
            # information, useless as an instruction.
            return {"base_yaw": None, "wrist": None, "source": "frame",
                    "confident": False, "why": "no pose recorded for the "
                    "frame this was seen in"}
        offset = (cx - self.frame_width / 2.0) / self.frame_width
        yaw = centre_yaw + offset * self.yaw_deg_per_frame
        wrist = None
        if cy is not None and centre_wrist is not None:
            oy = (cy - self.frame_height / 2.0) / self.frame_height
            wrist = centre_wrist + oy * self.wrist_deg_per_frame
        return {"base_yaw": yaw, "wrist": wrist, "source": "frame",
                "confident": True,
                "pose_recorded": pose is not None,
                "shape": None if pose is None else
                         {"shoulder": pose["shoulder"], "elbow": pose["elbow"]}}

    def last_seen(self, name, within_s=None):
        """The most recent verified sighting, or None."""
        if self.db is None:
            # Map-only: the dashboard owns the arm and the room map but not
            # the observer's sightings database.
            return None
        want = canonical(name)
        cutoff = 0.0
        if within_s is not None:
            cutoff = self.clock() - within_s
        row = self.db.execute(
            "SELECT * FROM sightings WHERE LOWER(label)=? AND ts >= ?"
            " ORDER BY ts DESC LIMIT 1", (want, cutoff)).fetchone()
        return dict(row) if row else None

    def find(self, name, within_s=86400):
        """Returns (sentence, yaw_or_None, found).

        `within_s` defaults to a day: BUILD_SPEC says "not seen today ->
        shake", so a sighting from last week is not an answer.
        """
        seen = self.last_seen(name, within_s=within_s)
        aim = self.aim(name, within_s=within_s)
        if seen is None:
            # The map may still know where it lives even with no sighting
            # today -- but "it is usually there" is not "I saw it", and
            # BUILD_SPEC is explicit that a guess from yesterday is worse
            # than an admission. So the sentence stays an admission and only
            # the direction is offered.
            if aim and aim.get("confident"):
                near = aim.get("near")
                where = f" It usually sits near {near}." if near else ""
                return ("Haven't seen it today." + where,
                        aim["base_yaw"], False)
            return "Haven't seen it today.", None, False
        when = time.strftime("%-I:%M", time.localtime(seen["ts"])) \
            if hasattr(time, "strftime") else ""
        zone = seen.get("zone") or "the desk"
        yaw = aim["base_yaw"] if aim and aim.get("confident") else None
        return f"{zone.capitalize()}, around {when}.", yaw, True
