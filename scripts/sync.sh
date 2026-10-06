#!/usr/bin/env bash
# Push the Mac's code to the MSI's avi\ folder over SFTP, in one connection.
#
# This replaces the base64-over-PowerShell transport in push-file.sh for bulk
# deploys. That transport sent ~6000 characters per SSH connection and spent
# about 14 seconds per file; the full tree took a quarter of an hour, and on a
# school network its chunks started timing out mid-file ("chunk 1 failed at
# offset 6000"), leaving a half-written module on a machine nobody was at.
#
# push-file.sh records two dead ends -- one oversized command-line argument,
# and piping to PowerShell's stdin, which hangs because Windows OpenSSH never
# propagates EOF. SFTP is neither. It is a separate subsystem of the same
# sshd: no PowerShell, so no quoting or escaping chain at all, no command-line
# length limit, and binary transfer instead of base64's 4/3 inflation.
#
# Paths are relative to the remote HOME on purpose. The account is
# "MSI-User", and a home-relative path keeps that space out of every
# filename rather than escaping it correctly in several places.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh

if ! msi_awake; then
  ./wake-msi.sh >/dev/null || { echo "could not wake MSI (not on the home LAN?)" >&2; exit 1; }
fi
./keepawake-msi.sh start 30 >/dev/null 2>&1 || true

BATCH=$(mktemp -t avi-sync)
MANIFEST=$(mktemp -t avi-manifest)
trap 'rm -f "$BATCH" "$MANIFEST"' EXIT

# `-` prefixes a line whose failure is ignored. mkdir on an existing directory
# fails, and there is no -Force here, so every mkdir is marked that way.
add_dir() { echo "-mkdir $1" >> "$BATCH"; }

add_tree() {
  local src="$1" dest="$2" rel
  [ -d "../$src" ] || return 0
  add_dir "avi/$dest"
  while IFS= read -r d; do
    d="${d#./}"
    [ "$d" = "." ] && continue
    add_dir "avi/$dest/$d"
  done < <(cd "../$src" && find . -type d ! -path '*__pycache__*' | sort)
  while IFS= read -r f; do
    rel="${f#../$src/}"
    echo "put \"$(cd .. && pwd)/${f#../}\" \"avi/$dest/$rel\"" >> "$BATCH"
    printf '%s\t%s\n' "avi/$dest/$rel" "$(wc -c < "$f" | tr -d ' ')" >> "$MANIFEST"
  done < <(find "../$src" -type f \( -name '*.py' -o -name '*.toml' -o -name '*.md' -o -name '*.ino' \) ! -path '*__pycache__*' | sort)
}

add_dir "avi"
add_tree "assistant" "assistant"
add_tree "avi_bot"   "avi_bot"
add_tree "config"    "config"

want=$(wc -l < "$MANIFEST" | tr -d ' ')
echo "sending $want file(s) over one sftp connection..."
start=$SECONDS
if ! sftp -q -o BatchMode=yes -o ConnectTimeout=15 -b "$BATCH" "$MSI_HOST" >/dev/null 2>"$BATCH.err"; then
  # A failed `put` is fatal; a failed `-mkdir` is not and is already ignored.
  if grep -qv 'mkdir' "$BATCH.err" 2>/dev/null && [ -s "$BATCH.err" ]; then
    echo "sftp reported:" >&2; grep -v 'mkdir' "$BATCH.err" >&2 | head -20
  fi
fi
took=$((SECONDS - start))

# Verify by SIZE, not by exit status. A transport that reports success while
# writing a short file is the specific failure this whole script exists to
# stop happening, and it has happened before: a deploy reported success per
# file while truncating at the command-line limit, and it cost two survey runs
# spent debugging code that had never arrived.
#
# The manifest goes over the same sftp connection and a -File script does the
# comparison on the MSI, so no part of this depends on a quoted PowerShell
# command line surviving the trip.
echo "--- size check ($took s for $want file(s)) ---"
printf 'put "%s" "avi-remote/manifest.tsv"\n' "$MANIFEST" > "$BATCH.m"
sftp -q -o BatchMode=yes -b "$BATCH.m" "$MSI_HOST" >/dev/null 2>&1 \
  || { echo "could not send the manifest" >&2; exit 1; }

ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
  "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\sizecheck.ps1\""
rc=$?
[ "$rc" -eq 0 ] || { echo "deploy is NOT trustworthy; see the mismatches above" >&2; exit 1; }

# Prove the import resolves, rather than trusting that bytes landed.
echo "--- import check on the MSI ---"
ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
  "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\$env:USERPROFILE\\avi-remote\\verify.ps1\"" 2>&1 | tail -4
