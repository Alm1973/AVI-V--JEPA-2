"""The dashboard's address for the Mac-side Python scripts.

$AVI_HOST if set, else MSI_HTTP from scripts/local.env (gitignored: the
repo is public, and the tailnet name is this machine's own), else the
MagicDNS short name.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def host():
    if os.environ.get("AVI_HOST"):
        return os.environ["AVI_HOST"].rstrip("/")
    try:
        with open(os.path.join(HERE, "local.env")) as f:
            m = re.search(r'^MSI_HTTP="?([^"\n]+)"?', f.read(), re.M)
        if m:
            return m.group(1).rstrip("/")
    except OSError:
        pass
    return "http://msi:8000"
