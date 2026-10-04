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
ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
  "Set-Location \"\$env:USERPROFILE\\avi\\assistant\"; & \"\$env:USERPROFILE\\avi\\.venv-yolo\\Scripts\\python.exe\" $*"
