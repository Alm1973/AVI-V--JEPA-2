#!/usr/bin/env bash
# Push the Mac's code to %USERPROFILE%\avi\ on the MSI.
# The Mac is the source of truth; never edit files on the MSI by hand.
#
#   assistant/  -> avi\assistant\   (runs beside the GPU modules it imports)
#   avi_bot/    -> avi\avi_bot\     (so "import avi_bot.motion" resolves)
#   config/     -> avi\config\      (where poses.py looks: ../config/joints.toml)
#
# Remote paths are written with FORWARD slashes on purpose. .NET and PowerShell
# both accept them, and it removes a whole class of backslash-escaping bugs from
# the bash -> ssh -> PowerShell chain: an earlier version lost every directory
# it tried to create and deployed 0 files while reporting success per file.
set -uo pipefail
cd "$(dirname "$0")"
source ./common.sh
SRC="../assistant"
REMOTE='$env:USERPROFILE/avi'

if [ ! -d "$SRC" ] || [ -z "$(ls -A "$SRC" 2>/dev/null)" ]; then
  echo "nothing to deploy: $(cd .. && pwd)/assistant is empty." >&2
  exit 1
fi

if ! msi_awake; then
  ./wake-msi.sh >/dev/null || { echo "could not wake MSI (not on the home LAN?)" >&2; exit 1; }
fi
./keepawake-msi.sh start 30 >/dev/null 2>&1 || true


# All single-file transfers go through push-file.sh, which chunks the base64
# under Windows' 32767-character command-line limit and verifies the byte count
# afterwards. See that script's header for the two approaches that do not work.
push_one() { ./push-file.sh "$1" "$2"; }

push_tree() {
  local src="$1" dest="$2" count=0 rel dir b64
  [ -d "$src" ] || return 0

  # Every directory first, in one round trip.
  local dirs=""
  while IFS= read -r d; do
    d="${d#./}"
    [ "$d" = "." ] && continue
    dirs="$dirs,'$d'"
  done < <(cd "$src" && find . -type d ! -path '*__pycache__*')
  ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
    "\$b=\"$REMOTE/$dest\"; New-Item -ItemType Directory -Force -Path \$b | Out-Null; foreach (\$d in @(''${dirs})) { if (\$d) { New-Item -ItemType Directory -Force -Path \"\$b/\$d\" | Out-Null } }" >/dev/null \
    || { echo "could not create directories under $dest" >&2; return 1; }

  while IFS= read -r f; do
    rel="${f#$src/}"
    b64=$(base64 < "$f" | tr -d '\n')
    if ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
         "[IO.File]::WriteAllBytes(\"$REMOTE/$dest/$rel\", [Convert]::FromBase64String('$b64'))"; then
      echo "  -> $dest/$rel"
      count=$((count+1))
    fi
  done < <(find "$src" -type f \( -name '*.py' -o -name '*.toml' -o -name '*.md' -o -name '*.ino' \) ! -path '*__pycache__*')
  echo "deployed $count file(s) to avi/$dest"
}

# assistant/ is flat, and deliberately excludes the model weights that live
# only on the MSI, so it keeps its own simple loop.
ssh "${SSH_OPTS[@]}" "$MSI_HOST" "New-Item -ItemType Directory -Force -Path \"$REMOTE/assistant\" | Out-Null" >/dev/null
n=0
for f in "$SRC"/*; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  push_one "$f" "assistant/$name" >/dev/null \
    && { echo "  -> assistant/$name"; n=$((n+1)); }
done
echo "deployed $n file(s) to $MSI_ASSISTANT"

push_tree "../avi_bot" "avi_bot" || exit 1
push_tree "../config"  "config"  || exit 1

# Verify the bytes actually landed. A silent short write is exactly what the
# command-line limit used to cause, and it looked like success.
echo "--- size check ---"
bad=0
for f in "$SRC"/*.py; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  local_n=$(wc -c < "$f" | tr -d ' ')
  remote_n=$(ssh -n "${SSH_OPTS[@]}" "$MSI_HOST" \
    "(Get-Item \"$REMOTE/assistant/$name\").Length" 2>/dev/null | tr -d '\r ')
  if [ "$local_n" != "$remote_n" ]; then
    echo "  MISMATCH $name: local $local_n, remote ${remote_n:-missing}" >&2
    bad=$((bad+1))
  fi
done
[ "$bad" -eq 0 ] && echo "  all assistant/*.py match byte-for-byte" || exit 1

# Prove the import actually resolves, rather than trusting that files landed.
echo "--- import check on the MSI ---"
ssh "${SSH_OPTS[@]}" "$MSI_HOST" \
  "& \"$REMOTE/.venv-yolo/Scripts/python.exe\" -c \"import os, sys; sys.path.insert(0, os.path.join(os.environ['USERPROFILE'], 'avi')); from avi_bot.motion import poses, gestures; c=poses.load(); print('avi_bot ok: tick', c.tick_hz, 'cap', c.speed_cap, len(gestures.GESTURES), 'gestures')\""
