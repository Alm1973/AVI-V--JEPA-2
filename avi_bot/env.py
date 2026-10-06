"""Load .env into the process environment. BUILD_SPEC 8.2: keys live in .env.

Deliberately not python-dotenv. This needs to run on a Windows box over SSH
inside a venv that already carries torch and ultralytics, and the whole job is
twenty lines -- a dependency for that is a dependency to keep working.

WHY THIS FILE EXISTS AT ALL
`providers.Chain` reads `os.environ`, and nothing was putting .env into it.
The key tested fine from the Mac, where it had been exported by hand, and
would have failed silently on the MSI -- the chain would have reported
"groq: no key", skipped straight to the local fallback, and looked exactly
like a rate limit. Found 2026-10-06 while deploying the key rather than after
wondering why the robot had got stupid.

Existing environment variables WIN. A key exported in a shell is a deliberate
override and should not be clobbered by a file.
"""
import os
import pathlib

FILENAME = ".env"


def find(start=None):
    """The nearest .env at or above `start`. Returns a Path or None."""
    here = pathlib.Path(start or __file__).resolve()
    if here.is_file():
        here = here.parent
    for folder in [here, *here.parents]:
        candidate = folder / FILENAME
        if candidate.is_file():
            return candidate
    return None


def parse(text):
    """KEY=value lines. Comments, blanks and `export ` prefixes tolerated."""
    out = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Strip one matched pair of surrounding quotes, and nothing else: a
        # key with a quote in the middle of it is a key, not a quoted string.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load(path=None, environ=None, override=False):
    """Put .env's non-empty values into the environment.

    Returns the list of KEY NAMES applied -- never the values, so this can be
    logged. A blank value is skipped rather than set to "", because
    `Chain.have_key` treats empty as absent and an empty string would make a
    provider look configured when it is not.
    """
    environ = environ if environ is not None else os.environ
    found = path or find()
    if found is None:
        return []
    found = pathlib.Path(found)
    # A path that was configured but is not there must not take startup down.
    # This runs before anything else on the MSI, where "the file moved" should
    # degrade to "no keys, local routes only" rather than to a traceback.
    if not found.is_file():
        return []
    applied = []
    for key, value in parse(found.read_text()).items():
        if not value:
            continue
        if key in environ and environ[key] and not override:
            continue
        environ[key] = value
        applied.append(key)
    return applied
