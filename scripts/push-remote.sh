#!/usr/bin/env bash
# Push the remote/*.ps1 helpers to the MSI. Uses base64 over ssh so the Windows
# path with a space never has to survive scp quoting.
set -euo pipefail
cd "$(dirname "$0")"
source ./common.sh
ssh "${SSH_OPTS[@]}" "$MSI_HOST" 'New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\avi-remote" | Out-Null' >/dev/null
for f in remote/*.ps1; do
  name=$(basename "$f")
  b64=$(base64 < "$f" | tr -d '\n')
  ssh "${SSH_OPTS[@]}" "$MSI_HOST" "[IO.File]::WriteAllBytes(\"\$env:USERPROFILE\\avi-remote\\$name\", [Convert]::FromBase64String('$b64')); Write-Output \"pushed $name (\$((Get-Item \"\$env:USERPROFILE\\avi-remote\\$name\").Length) bytes)\""
done
