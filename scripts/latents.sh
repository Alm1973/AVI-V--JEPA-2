#!/usr/bin/env bash
# Log V-JEPA latents of the desk as a scheduled task, so it survives ssh
# dropping. Usage: latents.sh start [hours] | stop | status | logs [n] | probe
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ACTION="${1:-status}"; shift || true
HOURS="${1:-8}"

ctl() {
  local argpart=""
  [ -n "${2:-}" ] && argpart="-ScriptArgs '$2'"
  ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
    "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\task-ctl.ps1\" -Action $1 -TaskName 'AVI latents' -Script latent_log.py $argpart ${3:+-Tail $3}"
}

case "$ACTION" in
  start)
    if msi_awake; then echo "MSI is already awake."
    else ./wake-msi.sh >/dev/null || { echo "could not wake the MSI" >&2; exit 1; }; fi
    ./keepawake-msi.sh start "$(( ${HOURS%.*} * 60 + 5 ))" >/dev/null 2>&1
    # One process owns the camera.
    for s in dashboard calibrate teleop stream tracker gcheck; do
      [ -x "./$s.sh" ] || continue
      if ./$s.sh status 2>/dev/null | grep -q "Running"; then
        echo "stopping $s ..."; ./$s.sh stop >/dev/null 2>&1
      fi
    done
    sleep 3
    ctl start "--hours $HOURS"
    ;;
  stop)   ctl stop ""; echo "stopped." ;;
  status) ctl status "" ;;
  logs)   ctl logs "" "${1:-40}" ;;
  probe)
    ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
      "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi\\assistant\\probe.ps1\"" ;;
  *) echo "usage: latents.sh start [hours]|stop|status|logs [n]|probe" >&2; exit 2 ;;
esac
