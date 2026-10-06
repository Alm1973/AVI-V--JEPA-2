#!/usr/bin/env bash
# Pose teaching on the real arm, from the Mac.
# Usage: calibrate.sh start [minutes] | sim [minutes] | stop | status | logs [n] | pull
#
#   start  real arm (sliders move actual servos)
#   sim    no serial port, for checking the page itself
#   pull   copy the taught poses back to the Mac
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ACTION="${1:-status}"; shift || true
MINUTES="${1:-45}"
PORT="${AVI_CALIB_PORT:-8082}"

ctl() {
  local argpart=""
  [ -n "${2:-}" ] && argpart="-ScriptArgs '$2'"
  ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
    "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\task-ctl.ps1\" -Action $1 -TaskName 'AVI calibrate' -Script calibrate.py $argpart ${3:+-Tail $3}"
}

free_the_camera() {
  # One process owns the C270 through DirectShow.
  for s in teleop stream tracker; do
    if ./$s.sh status 2>/dev/null | grep -q "Running"; then
      echo "stopping $s so the camera and the serial port are free ..."
      ./$s.sh stop >/dev/null 2>&1
    fi
  done
  sleep 3
}

start_it() {
  if msi_awake; then echo "MSI is already awake."
  else ./wake-msi.sh >/dev/null || { echo "could not wake the MSI" >&2; exit 1; }; fi
  ./keepawake-msi.sh start "$(( ${MINUTES%.*} + 3 ))" >/dev/null 2>&1
  free_the_camera
  ctl start "--port $PORT --minutes $MINUTES $1"
  echo
  echo "  open  http://${MSI_HOST}:${PORT}/"
  echo "  drag a slider, click save on a pose. './calibrate.sh pull' when done."
}

case "$ACTION" in
  start)  start_it "" ;;
  sim)    start_it "--backend sim" ;;
  stop)   ctl stop ""; echo "stopped." ;;
  status) ctl status "" ;;
  logs)   ctl logs "" "${1:-40}" ;;
  pull)
    out="../docs/taught_poses.json"
    ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
      'Get-Content "$env:USERPROFILE\avi-remote\taught_poses.json" -Raw' > "$out" 2>/dev/null \
      && python3 -c "
import json,sys
d=json.load(open('$out'))
if not d: print('nothing taught yet'); sys.exit(0)
for name in sorted(d):
    p=d[name]
    print(f\"{name:10} \" + '  '.join(f'{j}={p[j]:>6}' for j in
          ('base_yaw','shoulder','elbow','wrist')))
print(f'\n-> {len(d)} pose(s) written to docs/taught_poses.json')
" || echo "could not pull" >&2 ;;
  *) echo "usage: calibrate.sh start|sim [minutes]|stop|status|logs [n]|pull" >&2; exit 2 ;;
esac
