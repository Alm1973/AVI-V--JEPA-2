"""Launcher for the dashboard, which lives in avi_bot/dashboard/server.py.

It sits here because task-ctl.ps1 runs scheduled tasks out of avi\assistant\,
and a one-line shim is less fragile than teaching the task runner a second
directory layout.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.dashboard.server import main   # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
