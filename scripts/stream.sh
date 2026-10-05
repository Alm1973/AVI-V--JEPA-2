#!/usr/bin/env bash
# Live camera view over Tailscale, from the Mac.
# Usage: stream.sh start [minutes] | stop | status | logs [n]
#
# The camera is exclusive through DirectShow, so start stops the tracker first
# and stop offers it back. The stream has its own hard deadline on the MSI, so
# even if this Mac loses the tailnet the camera does not stay live.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ACTION="${1:-status}"; shift || true
MINUTES="${1:-15}"
PORT="${AVI_STREAM_PORT:-8080}"

ctl() {
  local argpart=""
  [ -n "${2:-}" ] && argpart="-ScriptArgs '$2'"
  ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
    "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\task-ctl.ps1\" -Action $1 -TaskName 'AVI stream' -Script camera_stream.py $argpart ${3:+-Tail $3}"
}

case "$ACTION" in
  start)
    # Already up (someone at the desk, or a tracker session) is the common case
    # when away from home, where wake-on-LAN cannot reach it at all.
    if msi_awake; then
      echo "MSI is already awake; skipping wake-on-LAN."
    else
    ./wake-msi.sh >/dev/null || {
      echo "could not wake the MSI." >&2
      echo "Wake-on-LAN only works while this Mac is on the home LAN" >&2
      echo "(192.168.0.0/24). See docs/HANDOFF.md, 'away-from-home wake'." >&2
      exit 1; }
    fi
    # A little longer than the stream, so the hold outlives it rather than the
    # machine dropping out mid-view. Bounded either way.
    ./keepawake-msi.sh start "$(( ${MINUTES%.*} + 3 ))" >/dev/null 2>&1
    # The tracker owns the camera when it is running. Take it, do not fight it.
    if ./tracker.sh status 2>/dev/null | grep -q "Running"; then
      echo "stopping the tracker so the camera is free ..."
      ./tracker.sh stop >/dev/null 2>&1
      sleep 3
    fi
    ctl start "--port $PORT --minutes $MINUTES"
    echo
    echo "  open  http://${MSI_HOST}:${PORT}/"
    echo "  it stops itself after ${MINUTES} min, or press stop on the page."
    ;;
  stop)
    ctl stop ""
    echo "stream stopped. The camera is free; './tracker.sh start' to resume logging."
    ;;
  status) ctl status "" ;;
  logs)   ctl logs "" "${1:-40}" ;;
  *) echo "usage: stream.sh start [minutes]|stop|status|logs [n]" >&2; exit 2 ;;
esac
