#!/usr/bin/env bash
# Shared config for the AVI desk remote scripts.
MSI_MAC="XX:XX:XX:XX:XX:XX"
MSI_TS_IP="100.x.y.z"
# For HTTP to the dashboard use the MagicDNS NAME, never the IP literal above.
# On an IPv6-only network -- found on a T-Mobile hotspot, 2026-10-06 -- macOS
# rewrites an IPv4 literal into a NAT64 IPv6 address (100.x.y.z became
# 2607:7700:0:32::644b:b126) and sends it out over cellular, straight past
# Tailscale, to a carrier gateway that cannot reach a 100.x address. curl then
# times out while ssh, sftp and `nc` (a raw IPv4 socket) all keep working, so
# it looks exactly like a hung dashboard. The name resolves through MagicDNS to
# an IPv4-only answer and goes through the tunnel. `curl -4` also works.
MSI_HTTP="http://msi.your-tailnet.ts.net:8000"
MSI_LAN_BCAST="192.168.0.255"
MSI_HOST="msi"                       # ~/.ssh/config alias
MSI_PROFILE='C:\Users\MSI-User'
MSI_AVI="$MSI_PROFILE\\avi"
MSI_VENV="$MSI_AVI\\.venv-yolo\\Scripts\\python.exe"
MSI_ASSISTANT="$MSI_AVI\\assistant"
MSI_REMOTE_SCRIPTS="$MSI_PROFILE\\avi-remote"   # our helpers; keeps avi/ repo clean
TAILSCALE="/usr/local/bin/tailscale"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)

ts_up() {
  if ! "$TAILSCALE" status >/dev/null 2>&1; then
    echo "tailscale is stopped; starting it..." >&2
    "$TAILSCALE" up --timeout=25s --accept-routes >/dev/null 2>&1 || true
  fi
}

msi_awake() { nc -z -G 3 "$MSI_TS_IP" 22 >/dev/null 2>&1; }
