#!/usr/bin/env bash
# Measure the MSI's CPU load with AVI's three MediaPipe senses running at once.
#   scripts/mp-bench.sh [seconds] [mjpg]   (default 60; sit in view for a realistic number)
# Wakes the MSI, frees the camera, pushes the bench, runs it, prints the table.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
SECS="${1:-60}"
MJPG=""; [ "${2:-}" = mjpg ] && MJPG="-Mjpg"
./wake-msi.sh >/dev/null || { echo "could not wake MSI" >&2; exit 1; }
./keepawake-msi.sh start 20 >/dev/null 2>&1 || true
../runHome stop >/dev/null 2>&1 || true          # only one process can hold the camera
./push-remote.sh >/dev/null || { echo "push of remote helpers failed" >&2; exit 1; }
./push-file.sh ../assistant/bench_mediapipe.py assistant/bench_mediapipe.py >/dev/null \
  || { echo "push of bench_mediapipe.py failed" >&2; exit 1; }
ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
  "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\mpbench.ps1\" -Seconds $SECS $MJPG"
