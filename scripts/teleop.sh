#!/usr/bin/env bash
# WASD remote controller + camera feed, from the Mac.
# Usage: teleop.sh start [minutes] | live [minutes] | stop | status | logs [n]
#
#   start  simulator backend (default, safe, the arm does not move)
#   live   serial backend -- MOVES THE REAL ARM. Needs firmware v2 flashed.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ACTION="${1:-status}"; shift || true
MINUTES="${1:-20}"
PORT="${AVI_TELEOP_PORT:-8081}"

ctl() {
  local argpart=""
  [ -n "${2:-}" ] && argpart="-ScriptArgs '$2'"
  ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
    "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\task-ctl.ps1\" -Action $1 -TaskName 'AVI teleop' -Script teleop.py $argpart ${3:+-Tail $3}"
}

free_the_camera() {
  # One process owns the C270 through DirectShow. Take it politely.
  for s in stream tracker; do
    if ./$s.sh status 2>/dev/null | grep -q "Running"; then
      echo "stopping $s so the camera is free ..."
      ./$s.sh stop >/dev/null 2>&1
    fi
  done
  sleep 3
}

start_it() {
  local extra="$1"
  if msi_awake; then
    echo "MSI is already awake."
  else
    ./wake-msi.sh >/dev/null || {
      echo "could not wake the MSI. Wake-on-LAN needs this Mac on the home" >&2
      echo "LAN (192.168.0.0/24). Wake timers bring it up every 2 hours:" >&2
      echo "  ./wake-timers.sh status" >&2
      exit 1; }
  fi
  ./keepawake-msi.sh start "$(( ${MINUTES%.*} + 3 ))" >/dev/null 2>&1
  free_the_camera
  ctl start "--port $PORT --minutes $MINUTES $extra"
  echo
  echo "  open  http://${MSI_HOST}:${PORT}/"
  echo "  click the page, then hold WASD. space = stop, H = home."
}

case "$ACTION" in
  start) start_it "" ;;
  live)
    cat >&2 <<'WARN'
LIVE MODE moves the real arm.

  - Opening COM5 resets the Arduino. Under the v1 firmware that snaps both
    servos to center, with nobody there to catch it.
  - teleop sends firmware v2 "P" frames. If v2 is NOT flashed, the arm will
    simply ignore everything and nothing will move.
  - The manual envelope is +-15 degrees, which is BUILD_SPEC's unattended cap.

WARN
    printf 'Somebody is watching the arm, and v2 is flashed. Type YES to go live: ' >&2
    read -r ans
    [ "$ans" = "YES" ] || { echo "aborted; nothing was opened." >&2; exit 1; }
    start_it "--backend serial --yes-move-the-arm" ;;
  stop)   ctl stop ""; echo "teleop stopped; camera free." ;;
  status) ctl status "" ;;
  logs)   ctl logs "" "${1:-40}" ;;
  *) echo "usage: teleop.sh start|live [minutes]|stop|status|logs [n]" >&2; exit 2 ;;
esac
