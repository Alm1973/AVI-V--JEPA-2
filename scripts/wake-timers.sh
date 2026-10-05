#!/usr/bin/env bash
# Wake timers on the MSI, so it can be reached while Shaurya is away from home.
# Usage: wake-timers.sh install [hold-minutes] | remove | status
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ACTION="${1:-status}"; HOLD="${2:-10}"
msi_awake || { echo "MSI is not reachable; wake timers can only be installed while it is up." >&2; exit 1; }
ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
  "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\wake-ctl.ps1\" -Action $ACTION -HoldMinutes $HOLD"
