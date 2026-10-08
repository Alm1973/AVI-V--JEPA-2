#!/usr/bin/env bash
# The dashboard over https, so the page's mic button can be used: browsers
# only give a page the microphone on a secure address. Tailscale serves its
# own certificate for msi.your-tailnet.ts.net and proxies to the dashboard,
# which keeps listening on the tailnet address as before.
#
# Needs HTTPS certificates switched on for the tailnet once (admin console
# -> DNS -> HTTPS Certificates). Then:
#   https.sh on      ->  https://msi.your-tailnet.ts.net/
#   https.sh off | status
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ACTION="${1:-status}"
PORT="${AVI_DASH_PORT:-8000}"
TS='& "C:\Program Files\Tailscale\tailscale.exe"'
case "$ACTION" in
  on)     ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
            "$TS serve --bg --https=443 http://100.x.y.z:$PORT" ;;
  off)    ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" "$TS serve --https=443 off" ;;
  status) ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" "$TS serve status" ;;
  *) echo "usage: https.sh on|off|status" >&2; exit 2 ;;
esac
