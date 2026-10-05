#!/usr/bin/env bash
# Room tracker control from the Mac: motion on -> logger runs, no motion -> GPU free.
# Usage: tracker.sh start [extra observer/tracker args] | stop | status | logs [n]
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ACTION="${1:-status}"; shift || true

ctl() {
  local argpart=""
  [ -n "${3:-}" ] && argpart="-ScriptArgs '$3'"
  ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
    "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\task-ctl.ps1\" -Action $1 -TaskName 'AVI tracker' -Script room_tracker.py $argpart ${4:+-Tail $4}"
}

case "$ACTION" in
  start)  ./wake-msi.sh >/dev/null || exit 1
          # The tracker decides when the GPU is busy, so it must outlive a long idle.
          ./keepawake-msi.sh start 480 >/dev/null 2>&1
          ctl start room_tracker.py "${*:-}" ;;
  stop)   ctl stop room_tracker.py ""
          # The logger is a child process; stop it too.
          ./observer.sh stop >/dev/null 2>&1 || true ;;
  status) ctl status room_tracker.py "" ;;
  logs)   ctl logs room_tracker.py "" "${1:-40}" ;;
  *) echo "usage: tracker.sh start|stop|status|logs" >&2; exit 2 ;;
esac
