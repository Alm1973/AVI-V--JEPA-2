#!/usr/bin/env bash
# Run a command and push its output to the `claude-runs` branch, so a cloud
# Claude session can read it with `git fetch` instead of it being copy-pasted.
#
#   ./scripts/report.sh ./scripts/mp-bench.sh 60
#
# Your working tree, current branch and uncommitted changes are NOT touched:
# the log is committed with git plumbing into a temporary index and pushed
# straight to refs/heads/claude-runs.
#
# THE REPO IS PUBLIC. Before anything is pushed, every personal value from
# scripts/local.env (Windows profile name, MAC, Tailscale IP, tailnet name) is
# replaced with the same placeholder CLAUDE.md uses. Don't use this for
# anything that prints keys or camera frames; nothing in scripts/ does.
set -uo pipefail
[ $# -ge 1 ] || { echo "usage: report.sh <command> [args...]" >&2; exit 2; }
cd "$(dirname "$0")/.."
BRANCH=claude-runs

out=$(mktemp); idx=$(mktemp); rm -f "$idx"
trap 'rm -f "$out" "$idx"' EXIT
{ echo "\$ $*"; echo "# $(date '+%Y-%m-%d %H:%M:%S %Z')"; } > "$out"
"$@" 2>&1 | tee -a "$out"
rc=${PIPESTATUS[0]}
echo "# exit $rc" >> "$out"

# Redact. Values come from local.env; the placeholders match CLAUDE.md.
if [ -f scripts/local.env ]; then
  # shellcheck disable=SC1091
  source scripts/local.env
  user="${MSI_PROFILE##*\\}"
  tailnet=$(printf '%s' "${MSI_HTTP:-}" | sed -nE 's#.*://[^.]+\.([^.:/]+\.ts\.net).*#\1#p')
  macdash="${MSI_MAC:-}"; macdash="${macdash//:/-}"
  for pair in "$user|MSI-User" "${MSI_MAC:-}|XX:XX:XX:XX:XX:XX" "$macdash|XX-XX-XX-XX-XX-XX" \
              "${MSI_TS_IP:-}|100.x.y.z" "$tailnet|your-tailnet.ts.net"; do
    from="${pair%%|*}"; to="${pair#*|}"
    [ -n "$from" ] || continue
    F="$from" T="$to" perl -pi -e 's/\Q$ENV{F}\E/$ENV{T}/gi' "$out"
  done
else
  echo "report.sh: no scripts/local.env, so nothing can be redacted; not pushing." >&2
  exit "$rc"
fi

name="runs/$(date +%Y-%m-%d_%H%M%S)-$$.log"
git fetch -q origin "$BRANCH" 2>/dev/null
parent=$(git rev-parse -q --verify "refs/remotes/origin/$BRANCH" 2>/dev/null || true)
export GIT_INDEX_FILE="$idx"
[ -n "$parent" ] && git read-tree "$parent"
blob=$(git hash-object -w "$out")
git update-index --add --cacheinfo "100644,$blob,$name"
tree=$(git write-tree)
# The message is the REDACTED command line, never "$*" itself.
commit=$(git commit-tree "$tree" ${parent:+-p "$parent"} -m "run: $(head -1 "$out" | cut -c3-)")
unset GIT_INDEX_FILE
if git push -q origin "$commit:refs/heads/$BRANCH"; then
  echo; echo "uploaded $name to $BRANCH (exit $rc). Tell Claude: done"
else
  echo "push failed; paste the output instead" >&2
fi
exit "$rc"
