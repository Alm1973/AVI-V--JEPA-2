#!/usr/bin/env bash
# The AVI dashboard: camera, joints, poses, gestures, in one page.
# Usage: dashboard.sh start [minutes] | sim [minutes] | stop | status | logs [n]
#        dashboard.sh restart   (stop, then run until 22:00 / $AVI_DAY_END)
#        dashboard.sh daily [HH:MM] [minutes] | undaily
#   daily: Windows starts it every day at HH:MM (default 08:10) for [minutes]
#   (default 830, i.e. to ~22:00), keeping the MSI awake only while it runs.
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
  restart)
    # A redeploy mid-day: run to the daily end (22:00), not a fixed hour --
    # "start 300" at 08:27 cut the day's run off at 13:27.
    END="${AVI_DAY_END:-22:00}"
    now=$(( 10#$(date +%H) * 60 + 10#$(date +%M) ))
    end=$(( 10#${END%:*} * 60 + 10#${END#*:} ))
    MINUTES=$(( end - now )); [ "$MINUTES" -lt 60 ] && MINUTES=60
    ctl stop "" >/dev/null; echo "restarting for $MINUTES min (to $END)"
    start_it "" ;;
  daily)
    AT="${1:-08:10}"; MIN="${2:-830}"
    ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
      "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\task-ctl.ps1\" -Action daily -TaskName 'AVI dashboard' -Script dashboard.py -At '$AT' -ScriptArgs '--port $PORT --minutes $MIN --keep-awake'" ;;
  undaily) ctl undaily "" ;;
  status) ctl status "" ;;
  logs)   ctl logs "" "${1:-40}" ;;
  *) echo "usage: dashboard.sh start|sim [minutes]|stop|restart|status|logs [n]|daily [HH:MM] [minutes]|undaily" >&2; exit 2 ;;
esac
