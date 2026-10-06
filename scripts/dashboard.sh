#!/usr/bin/env bash
# The AVI dashboard: camera, joints, poses, gestures, in one page.
# Usage: dashboard.sh start [minutes] | sim [minutes] | stop | status | logs [n]
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ACTION="${1:-status}"; shift || true
MINUTES="${1:-60}"
PORT="${AVI_DASH_PORT:-8000}"

ctl() {
  local argpart=""
  [ -n "${2:-}" ] && argpart="-ScriptArgs '$2'"
  ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
    "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\task-ctl.ps1\" -Action $1 -TaskName 'AVI dashboard' -Script dashboard.py $argpart ${3:+-Tail $3}"
}

start_it() {
  if msi_awake; then echo "MSI is already awake."
  else ./wake-msi.sh >/dev/null || { echo "could not wake the MSI" >&2; exit 1; }; fi
  ./keepawake-msi.sh start "$(( ${MINUTES%.*} + 3 ))" >/dev/null 2>&1
  # One process owns the camera and the serial port.
  for s in calibrate teleop stream tracker gcheck; do
    [ -x "./$s.sh" ] || continue
    if ./$s.sh status 2>/dev/null | grep -q "Running"; then
      echo "stopping $s ..."; ./$s.sh stop >/dev/null 2>&1
    fi
  done
  sleep 3
  ctl start "--port $PORT --minutes $MINUTES $1"
  echo
  echo "  open  http://${MSI_HOST}:${PORT}/"
}

case "$ACTION" in
  start)  start_it "" ;;
  sim)    start_it "--backend sim" ;;
  stop)   ctl stop ""; echo "stopped." ;;
  status) ctl status "" ;;
  logs)   ctl logs "" "${1:-40}" ;;
  *) echo "usage: dashboard.sh start|sim [minutes]|stop|status|logs [n]" >&2; exit 2 ;;
esac
