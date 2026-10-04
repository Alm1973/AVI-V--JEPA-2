#!/usr/bin/env bash
# Shared config for the AVI desk remote scripts.
MSI_MAC="XX:XX:XX:XX:XX:XX"
MSI_TS_IP="100.x.y.z"
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
