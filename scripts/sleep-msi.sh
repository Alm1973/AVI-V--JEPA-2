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

# CRITICAL: the Ethernet NIC has WakeOnPattern enabled, so an incoming TCP SYN
# wakes the machine. An eager confirmation loop probing port 22 every few seconds
# wakes it back up as fast as it sleeps - the earlier version of this script was
# fighting itself and reporting "it may not have slept".
# So: wait quietly with NO packets, then probe sparingly.
echo "waiting quietly (probing would wake it again) ..."
sleep 45
# SetSuspendState does not always fire. Observed once: nothing holding a SYSTEM
# power request, keep-awake released, yet the machine stayed up; a re-issue slept
# it immediately. So re-issue rather than just probing again.
for attempt in 1 2 3; do
  if ! msi_awake; then echo "MSI is asleep."; exit 0; fi
  if [ "$attempt" -lt 3 ]; then
    echo "  still up after attempt $attempt; re-issuing sleep"
    ssh -o BatchMode=yes -o ConnectTimeout=10 \
        -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
        "$MSI_HOST" 'Start-Process rundll32.exe -ArgumentList "powrprof.dll,SetSuspendState","0,1,0"' \
        >/dev/null 2>&1 || true
    sleep 40
  fi
done
echo "MSI still reachable. Note: each probe can wake it while WakeOnPattern is enabled," >&2
echo "so this check is not fully reliable. To make sleep stick, consider:" >&2
echo "  Set-NetAdapterPowerManagement -Name Ethernet -WakeOnPattern Disabled" >&2
echo "(keeps WakeOnMagicPacket, so wake-msi.sh still works)." >&2
exit 1
