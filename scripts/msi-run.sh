#!/usr/bin/env bash
# Run a python file (or module args) from assistant/ on the MSI with the GPU venv.
# Usage: msi-run.sh test_observer.py [args...]
#        msi-run.sh observer.py --once
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
[ $# -ge 1 ] || { echo "usage: msi-run.sh <script.py> [args...]" >&2; exit 2; }
./wake-msi.sh >/dev/null || { echo "could not wake MSI" >&2; exit 1; }
./keepawake-msi.sh start 30 >/dev/null 2>&1 || true
# -Stop on the Set-Location, so a missing assistant\ dir fails loudly instead of
# silently running in the profile directory and reporting nonsense.
ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
  "\$ErrorActionPreference='Stop'; Set-Location \"\$env:USERPROFILE\\avi\\assistant\"; & \"\$env:USERPROFILE\\avi\\.venv-yolo\\Scripts\\python.exe\" $*"
rc=$?
if [ $rc -ne 0 ]; then
  echo "msi-run.sh failed (exit $rc). If the error mentions PathNotFound, assistant/ is not deployed yet - run ./deploy.sh." >&2
fi
exit $rc
