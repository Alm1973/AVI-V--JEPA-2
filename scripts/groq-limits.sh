#!/usr/bin/env bash
# Groq's live free-tier limits for each brain model, read from the API's own
# response headers rather than a blog. One 8-token request per model.
#
#   scripts/groq-limits.sh
#
# Needs GROQ_API_KEY: exported, or in the repo's .env (where avi_bot/env.py
# reads it). Groq's header meaning, per its rate-limit docs:
#   x-ratelimit-limit-requests  = requests per DAY
#   x-ratelimit-limit-tokens    = tokens per MINUTE
# Tokens per DAY is not in the headers; the console shows it:
#   https://console.groq.com/settings/limits
set -uo pipefail
cd "$(dirname "$0")/.."
if [ -z "${GROQ_API_KEY:-}" ] && [ -f .env ]; then
  GROQ_API_KEY=$(grep -E '^(export )?GROQ_API_KEY=' .env | tail -1 | cut -d= -f2- | tr -d "\"' ")
fi
: "${GROQ_API_KEY:?no GROQ_API_KEY exported or in .env}"

body=$(mktemp); trap 'rm -f "$body"' EXIT
for m in openai/gpt-oss-20b openai/gpt-oss-120b qwen/qwen3.8-27b; do
  echo "== $m"
  curl -sS -D - -o "$body" https://api.groq.com/openai/v1/chat/completions \
    -H "Authorization: Bearer $GROQ_API_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_completion_tokens\":8}" \
    | grep -i -E '^(HTTP/|x-ratelimit-)'
  # An error body (model withdrawn, not free) says why; print it.
  grep -o '"message":"[^"]*"' "$body" | head -1
done
echo
echo "limit-requests = per day, limit-tokens = per minute. Tokens/day: https://console.groq.com/settings/limits"
