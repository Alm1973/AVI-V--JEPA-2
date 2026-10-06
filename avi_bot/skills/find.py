"""Where something was last seen. BUILD_SPEC 9.

The acceptance test is "a seeded database gives the right answer and the right
yaw", and the yaw half is what makes this a robot skill rather than a database
query: after answering, AVI should LOOK at where the thing was, which means
turning a stored image position back into a joint angle.

That conversion uses the measured `yaw_deg_per_frame` (83.9, see
config/joints.toml), and it is the one place a wrong constant would be
invisible -- the sentence would still be right while the arm pointed at the
wrong part of the room.

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
                 centre_yaw=90.0, clock=time.time):
        self.db = db
        self.yaw_deg_per_frame = float(yaw_deg_per_frame)
        self.frame_width = float(frame_width)
        self.centre_yaw = float(centre_yaw)
        self.clock = clock

    def yaw_for(self, cx):
        """Image x -> the base_yaw that would centre it.

        Raising base_yaw moves a fixed landmark LEFT (measured, r2 = 0.976), so
        something to the RIGHT of centre needs MORE yaw.
        """
        offset = (float(cx) - self.frame_width / 2.0) / self.frame_width
        return self.centre_yaw + offset * self.yaw_deg_per_frame

    def last_seen(self, name, within_s=None):
        """The most recent verified sighting, or None."""
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
        if seen is None:
            return "Haven't seen it today.", None, False
        when = time.strftime("%-I:%M", time.localtime(seen["ts"])) \
            if hasattr(time, "strftime") else ""
        zone = seen.get("zone") or "the desk"
        cx = seen.get("cx")
        yaw = self.yaw_for(cx) if cx is not None else None
        return f"{zone.capitalize()}, around {when}.", yaw, True
