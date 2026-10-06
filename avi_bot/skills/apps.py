"""Opening apps, from a whitelist and nothing else. BUILD_SPEC 9.

This is a tool an LLM can call with a string it made up, so without a
whitelist "open calculator" and "open any executable on the disk" are the same
capability. The acceptance test is that an unlisted app is refused, and
`open_app` therefore returns a refusal rather than raising: a refused app is a
normal outcome the robot should shrug at, not an error worth narrating.
"""
import os
import re
import tomllib

DEFAULT_CONFIG = os.path.join("config", "apps.toml")

# A URL we are willing to hand to the browser. http(s) only: a file:// or a
# custom scheme from a model's string is a different and larger capability.
_URL = re.compile(r"^https?://", re.I)


class Apps:
    def __init__(self, path=DEFAULT_CONFIG, table=None, launch=None):
        if table is not None:
            self.table = {k.lower(): v for k, v in table.items()}
        else:
            self.table = self._load(path)
        # Injected so the whitelist logic is testable without launching
        # anything, which is the part that actually needs testing.
        self.launch = launch or self._real_launch

    @staticmethod
    def _load(path):
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            return {}
        return {k.lower(): v for k, v in (data.get("apps") or {}).items()}

    def known(self):
        return sorted(self.table)

    def resolve(self, name):
        """The target for a name, or None if it is not on the list."""
        return self.table.get(" ".join((name or "").split()).lower())

    def open(self, name):
        """Open a whitelisted app. Returns (ok, message)."""
        target = self.resolve(name)
        if target is None:
            return False, f"{name!r} is not on the list"
        try:
            self.launch(target)
        except Exception as exc:
            return False, f"could not open {name}: {type(exc).__name__}"
        return True, f"opened {name}"

    @staticmethod
    def _real_launch(target):
        if _URL.match(target):
            import webbrowser
            webbrowser.open(target)
            return
        # Not shell=True, and no string interpolation: the target comes from
        # the config file, but keeping the call shape safe means a future edit
        # to that file cannot become command injection.
        import subprocess
        subprocess.Popen([target], close_fds=True)
