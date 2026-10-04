#!/usr/bin/env bash
# One-glance view: tailscale, reachability, GPU, keep-awake.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ts_up
echo "=== tailscale ==="; "$TAILSCALE" status | grep -iE "msi|macbook"
if ! msi_awake; then echo "=== MSI: ASLEEP (run ./wake-msi.sh) ==="; exit 0; fi
echo "=== MSI: awake ==="
ssh "${SSH_OPTS[@]}" "$MSI_HOST" 'hostname; nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader' 2>/dev/null
echo "=== keep-awake ==="
./keepawake-msi.sh status 2>/dev/null | head -3
