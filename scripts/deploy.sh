#!/usr/bin/env bash
# Push the Mac's assistant/ to C:\Users\MSI-User\avi\assistant\ on the MSI.
# The Mac is the source of truth; never edit files on the MSI by hand.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
SRC="../assistant"

if [ ! -d "$SRC" ] || [ -z "$(ls -A "$SRC" 2>/dev/null)" ]; then
  echo "nothing to deploy: $(cd .. && pwd)/assistant is empty." >&2
  echo "Unpack assistant.zip into it first." >&2
  exit 1
fi

./wake-msi.sh >/dev/null || { echo "could not wake MSI" >&2; exit 1; }
./keepawake-msi.sh start 30 >/dev/null 2>&1 || true

ssh "${SSH_OPTS[@]}" "$MSI_HOST" 'New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\avi\assistant" | Out-Null' >/dev/null

n=0
for f in "$SRC"/*; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  b64=$(base64 < "$f" | tr -d '\n')
  ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
    "[IO.File]::WriteAllBytes(\"\$env:USERPROFILE\\avi\\assistant\\$name\", [Convert]::FromBase64String('$b64'))" \
    && { echo "  -> $name"; n=$((n+1)); }
done
echo "deployed $n file(s) to $MSI_ASSISTANT"
ssh "${SSH_OPTS[@]}" "$MSI_HOST" 'Get-ChildItem "$env:USERPROFILE\avi\assistant" | Select-Object Name,Length | Format-Table -AutoSize'
