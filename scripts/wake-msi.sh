#!/usr/bin/env bash
# Wake the MSI with a Wake-on-LAN magic packet, then wait for sshd.
# Requires the MSI's Ethernet cable plugged in. Confirmed working 2026-10-04 (8 s).
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ts_up

if msi_awake; then echo "MSI already awake (sshd reachable)."; exit 0; fi

ON_HOME_LAN=$(ipconfig getifaddr en0 2>/dev/null | grep -q '^192\.168\.0\.' && echo yes || echo no)
if [ "$ON_HOME_LAN" = "no" ]; then
  echo "WARNING: this Mac is not on 192.168.0.0/24, so a broadcast magic packet will not reach the MSI." >&2
  echo "         A wake relay on the home network is needed (Raspberry Pi on Tailscale, or router WoL button)." >&2
fi

echo "sending magic packet to $MSI_MAC ..."
python3 ./wol.py "$MSI_MAC" "$MSI_LAN_BCAST:9" "$MSI_LAN_BCAST:7"

DEADLINE=$(( $(date +%s) + 90 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if msi_awake; then
    echo "MSI awake after $(( 90 - (DEADLINE - $(date +%s)) ))s."
    ssh "${SSH_OPTS[@]}" "$MSI_HOST" 'hostname' 2>/dev/null && exit 0
    exit 0
  fi
  sleep 3
done
echo "MSI did not wake within 90s." >&2
echo "Check: Ethernet cable plugged in; Energy Efficient Ethernet off; 'Wake on Magic Packet' enabled." >&2
exit 1
