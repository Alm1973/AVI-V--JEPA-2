#!/usr/bin/env bash
# Usage: keepawake-msi.sh start [minutes] | stop | status
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ACTION="${1:-status}"; MINUTES="${2:-60}"
ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
  "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\keepawake-ctl.ps1\" -Action $ACTION -Minutes $MINUTES"
