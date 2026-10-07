"""Small switches that must survive a dashboard restart.

Every restart used to switch latent recording OFF silently: fifteen restarts
on 2026-10-06 meant an afternoon of V-JEPA data that was never taken, and
nothing said so. A switch someone turned on stays on until someone turns it
off.

No heavy imports, so it is testable on the Mac.
"""
import json
import os


def path():
    d = os.path.join(os.environ.get("USERPROFILE", "."), "avi-remote")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "dashboard-settings.json")


def load(p):
    """The saved switches, or {} -- never raises: a broken settings file
    must not stop the robot starting."""
    try:
        with open(p) as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def save(p, key, value):
    """Set one switch, atomically: a crash mid-write leaves the old file."""
    got = load(p)
    got[key] = value
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(got, f, indent=1)
    os.replace(tmp, p)
