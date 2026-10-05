#!/usr/bin/env bash
# Push one local file to a path under the MSI's avi\ folder.
#   push-file.sh ../assistant/teleop.py assistant/teleop.py
#
# Sends base64 in chunks small enough for a Windows command line, then decodes
# server-side and verifies the byte count.
#
# Two dead ends, recorded so neither gets retried:
#
#   One big argument. Windows caps a command line at 32767 characters, and
#   base64 of a 24 KB source is ~32 KB. teleop.py silently stopped
#   transferring at 594 lines while every push still reported success.
#
#   Piping over ssh stdin with [Console]::In.ReadToEnd(). One round trip and
#   clean in principle; it HANGS, because Windows OpenSSH does not propagate
#   EOF to PowerShell, so the read never returns.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh

SRC="${1:?usage: push-file.sh <local> <remote-path-under-avi>}"
DEST="${2:?usage: push-file.sh <local> <remote-path-under-avi>}"
CHUNK="${PUSH_CHUNK:-6000}"
TMP='$env:TEMP\avi_push.b64'
TARGET="\$env:USERPROFILE\\avi\\${DEST//\//\\}"

[ -f "$SRC" ] || { echo "no such file: $SRC" >&2; exit 1; }

b64=$(base64 < "$SRC" | tr -d '\n')
len=${#b64}

# Make sure the directory exists before writing into it.
dir=$(dirname "$DEST")
if [ "$dir" != "." ]; then
  ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
    "New-Item -ItemType Directory -Force -Path \"\$env:USERPROFILE\\avi\\${dir//\//\\}\" | Out-Null" \
    || { echo "could not create $dir" >&2; exit 1; }
fi

# `exit 0` on the end deliberately. Remove-Item on a path that does not exist
# returns a FAILING exit status over ssh even with -ErrorAction
# SilentlyContinue, so without this the very first push of a session aborts --
# and it only looked fine while a stale staging file happened to be lying
# around from an earlier run.
ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
  "if (Test-Path \"$TMP\") { Remove-Item \"$TMP\" -Force }; exit 0" \
  || { echo "could not clear the staging file" >&2; exit 1; }

i=0
n=0
while [ "$i" -lt "$len" ]; do
  part=${b64:$i:$CHUNK}
  ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
    "Add-Content -Path \"$TMP\" -Value '$part' -NoNewline -Encoding ascii" \
    || { echo "chunk $n failed at offset $i" >&2; exit 1; }
  i=$((i + CHUNK))
  n=$((n + 1))
done

ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
  "[IO.File]::WriteAllBytes(\"$TARGET\", [Convert]::FromBase64String((Get-Content \"$TMP\" -Raw).Trim())); Remove-Item \"$TMP\" -Force" \
  || { echo "decode failed" >&2; exit 1; }

# Verify, rather than trusting that a write happened: a silent short write is
# exactly what the command-line limit used to produce, and it looked like
# success.
local_n=$(wc -c < "$SRC" | tr -d ' ')
remote_n=$(ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
  "(Get-Item \"$TARGET\").Length" 2>/dev/null | tr -d '\r ')
if [ "$local_n" != "$remote_n" ]; then
  echo "MISMATCH $DEST: local $local_n, remote ${remote_n:-missing}" >&2
  exit 1
fi
echo "pushed $DEST  ($local_n bytes, $n chunks)"
