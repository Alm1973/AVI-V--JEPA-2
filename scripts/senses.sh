#!/usr/bin/env bash
# AVI's senses (docs/REDESIGN.md step 1) on the MSI, from the Mac.
#
#   senses.sh once [seconds]     a foreground test run (default 60), events printed live
#   senses.sh start [hours]      run as the "AVI senses" task (keep-awake for `hours`, default 4)
#   senses.sh stop | status | logs [n]
#   senses.sh report [hours]     events and row counts from senses-dev.db (default 24)
#
# Every action first pushes the senses code (5 files, seconds -- not a full
# deploy). Runs in avi-desk\.conda312, made by mp-bench.sh; never .venv-yolo.
# Takes the camera, so the dashboard is stopped first.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh

ACTION="${1:-status}"; shift || true
PY='$env:USERPROFILE\avi-desk\.conda312\python.exe'
SCRIPT='$env:USERPROFILE\avi\assistant\senses.py'

push() {
  ./push-remote.sh >/dev/null || { echo "push of remote helpers failed" >&2; exit 1; }
  for f in assistant/senses.py avi_bot/__init__.py avi_bot/senses/__init__.py \
           avi_bot/senses/events.py avi_bot/senses/store.py; do
    ./push-file.sh "../$f" "$f" >/dev/null || { echo "push of $f failed" >&2; exit 1; }
  done
}
ctl() {  # ctl <Action> [ScriptArgs] [Tail]
  local argpart=""
  [ -n "${2:-}" ] && argpart="-ScriptArgs '$2'"
  ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
    "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\task-ctl.ps1\" -Action $1 -TaskName 'AVI senses' -Script senses.py -Venv desk $argpart ${3:+-Tail $3}"
}
fg() {  # a short foreground run over plain ssh. Exit is python's own: MediaPipe's
        # stderr warnings otherwise made PowerShell report 1 on a clean run.
  ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
    "if (-not (Test-Path \"$PY\")) { 'no Python 3.12 env yet: run ./scripts/mp-bench.sh 20 once'; exit 1 }; Set-Location \"\$env:USERPROFILE\\avi\\assistant\"; & \"$PY\" -u \"$SCRIPT\" $* 2>&1 | ForEach-Object { \"\$_\" } | Where-Object { \$_ -notmatch '^(W|I)0000|inference_feedback|^WARNING: Logging|^INFO: Created' }; exit \$LASTEXITCODE"
}

./wake-msi.sh >/dev/null || { echo "could not wake MSI (not on the home LAN?)" >&2; exit 1; }
case "$ACTION" in
  once)   ./keepawake-msi.sh start 20 >/dev/null 2>&1 || true
          push; ../runHome stop >/dev/null 2>&1 || true
          fg --seconds "${1:-60}" ;;
  start)  ./keepawake-msi.sh start "$(( ${1:-4} * 60 ))" >/dev/null 2>&1 || true
          push; ../runHome stop >/dev/null 2>&1 || true
          ctl start "--profile dev" ;;
  stop)   ctl stop ;;
  status) ctl status ;;
  logs)   ctl logs "" "${1:-40}" ;;
  report) push; fg --report "${1:-24}" ;;
  *) echo "usage: senses.sh once|start|stop|status|logs|report" >&2; exit 2 ;;
esac
