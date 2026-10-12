#!/usr/bin/env bash
# Can the R820 run gpt-oss-20b fast enough to be AVI's unlimited background
# brain? Builds llama.cpp on the R820, downloads the 12 GB MXFP4 GGUF once,
# and runs llama-bench at the size of a real brain call.
#
#   scripts/r820-bench.sh            # ssh host alias "r820" (see ~/.ssh/config)
#   R820_HOST=user@100.x.y.z scripts/r820-bench.sh
#
# Prerequisite (docs/R820-NODE.md): Ubuntu on the R820, on the tailnet, and
#   sudo apt install -y build-essential cmake git curl
#
# WHAT THE NUMBERS MEAN
# A brain call is ~2.3K tokens in (providers.py, measured) and up to ~300 out.
# On a CPU the INPUT side (pp) is usually the slow part, so both are measured:
#   pp2048 = prompt tokens/s   ->  seconds to read a call = 2300 / pp
#   tg300  = output tokens/s   ->  seconds to write a reply = 300 / tg
# Background work (nightly reflection, non-urgent decisions) can wait minutes.
# Live conversation cannot, and is not what this box is for.
set -uo pipefail
HOST="${R820_HOST:-r820}"

ssh -o ServerAliveInterval=30 "$HOST" 'bash -s' <<'REMOTE'
set -uo pipefail
for t in git cmake g++ curl; do
  command -v "$t" >/dev/null || { echo "missing $t: sudo apt install -y build-essential cmake git curl"; exit 1; }
done
echo "== machine"; lscpu | grep -E 'Model name|^CPU\(s\)|Socket|NUMA node\(s\)'; free -g | head -2

cd ~
[ -d llama.cpp ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp
cd llama.cpp && git pull -q --ff-only || true
echo "== llama.cpp $(git rev-parse --short HEAD)"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON >/dev/null \
  && cmake --build build -j"$(nproc)" --target llama-bench >/dev/null \
  || { echo "build failed"; exit 1; }

M=~/models/gpt-oss-20b-MXFP4.gguf
mkdir -p ~/models
if [ ! -s "$M" ] || [ "$(stat -c %s "$M")" -ne 12109566624 ]; then
  echo "== downloading 12.1 GB model (resumable)"
  curl -L -C - -o "$M" https://huggingface.co/ggml-org/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-MXFP4.gguf
fi
[ "$(stat -c %s "$M")" -eq 12109566624 ] || { echo "model size wrong, re-run to resume"; exit 1; }

N=$(nproc)
LOG=~/r820-bench-$(date +%F-%H%M).md
echo "== bench (threads $((N/2)),$N; with and without NUMA distribute) -> $LOG"
{
  ./build/bin/llama-bench -m "$M" -p 2048 -n 300 -t "$((N/2)),$N" -r 2 -o md
  ./build/bin/llama-bench -m "$M" -p 2048 -n 300 -t "$N" -r 2 --numa distribute -o md
} 2>/dev/null | tee "$LOG"
echo
echo "seconds per brain call ~= 2300/pp2048 + 300/tg300 (best row)"
REMOTE
