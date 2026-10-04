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
ssh "${SSH_OPTS[@]}" "$MSI_HOST" 'rundll32.exe powrprof.dll,SetSuspendState 0,1,0' >/dev/null 2>&1 || true

DEADLINE=$(( $(date +%s) + 45 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if ! msi_awake; then echo "MSI is asleep."; exit 0; fi
  sleep 3
done
echo "MSI still reachable after 45s — it may not have slept. Check hibernate/keep-awake state." >&2
exit 1
