#!/usr/bin/env bash
# Phase 4 control surface for the observation logger, from the Mac.
# Usage: observer.sh start|stop|status|logs|today|once|test [extra args...]
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh

ACTION="${1:-status}"; shift || true
ctl() {  # ctl <Action> <Script> <ScriptArgs> [Tail]
  # An empty -ScriptArgs '' is stripped by the bash->ssh->PowerShell layers and
  # PowerShell then complains the parameter is missing, so omit it when empty.
  local argpart=""
  [ -n "${3:-}" ] && argpart="-ScriptArgs '$3'"
  ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
    "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\task-ctl.ps1\" -Action $1 -Script $2 $argpart ${4:+-Tail $4}"
}

case "$ACTION" in
  start)  ./wake-msi.sh >/dev/null || exit 1
          ./keepawake-msi.sh start 240 >/dev/null 2>&1
          ctl start observer.py "--no-preview ${*:-}" ;;
  stop)   ctl stop observer.py "" ;;
  status) ctl status observer.py "" ;;
  logs)   ctl logs observer.py "" "${1:-40}" ;;
  # Short foreground runs: plain ssh is fine, no task needed.
  once)   ./msi-run.sh observer.py --once "${@:-}" ;;
  today)  ./msi-run.sh today.py "${@:-}" ;;
  test)   ./msi-run.sh test_observer.py ;;
  *) echo "usage: observer.sh start|stop|status|logs|today|once|test" >&2; exit 2 ;;
esac
