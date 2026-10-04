#!/usr/bin/env bash
# Put the MSI back to sleep. Stop the observer BEFORE calling this.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
ts_up

if ! msi_awake; then echo "MSI is already unreachable (likely asleep)."; exit 0; fi

# Release any keep-awake hold first, otherwise Windows may refuse to idle-sleep.
./keepawake-msi.sh stop >/dev/null 2>&1 || true

echo "sleeping MSI ..."
# The machine dies underneath this connection, so ssh never gets a clean close.
# Short keepalives make it give up in ~10s instead of inheriting the 30x6=180s
# from ~/.ssh/config, and the command is detached so sshd does not wait on it.
ssh -o BatchMode=yes -o ConnectTimeout=10 \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    "$MSI_HOST" 'Start-Process rundll32.exe -ArgumentList "powrprof.dll,SetSuspendState","0,1,0"' \
    >/dev/null 2>&1 || true

# Windows takes a variable amount of time to finish the suspend transition, and
# the network stays up for part of it. 45s produced false failures; 120s does not.
DEADLINE=$(( $(date +%s) + 120 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if ! msi_awake; then echo "MSI is asleep (took $(( 120 - (DEADLINE - $(date +%s)) ))s)."; exit 0; fi
  sleep 3
done
echo "MSI still reachable after 120s - it may not have slept." >&2
echo "Check: keep-awake still held (./keepawake-msi.sh status), or an app holding a SYSTEM power request (powercfg /requests)." >&2
exit 1
