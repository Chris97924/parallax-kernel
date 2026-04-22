#!/usr/bin/env bash
# Parallax v0.6 Phase A — Hackathon demo walkthrough.
#
# One-shot script for judges: starts the server, ingests a bit of
# knowledge, demonstrates "session 不失憶" by pulling the reminder block
# a fresh Claude session would see.
#
# Prereqs:
#   pip install -e '.[server]'
#   (optional) export PARALLAX_TOKEN=t0ken
#
# Usage: bash demo/hackathon_walkthrough.sh

set -euo pipefail

PORT="${PARALLAX_PORT:-8765}"
BASE="http://127.0.0.1:${PORT}"
USER_ID="${PARALLAX_USER_ID:-chris}"
TOKEN_HEADER=()
if [[ -n "${PARALLAX_TOKEN:-}" ]]; then
  TOKEN_HEADER=(-H "Authorization: Bearer ${PARALLAX_TOKEN}")
fi

say() { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }
log() { printf "   %s\n" "$*"; }

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

say "Starting parallax serve on :${PORT}"
parallax serve --host 127.0.0.1 --port "${PORT}" --log-level warning &
SERVER_PID=$!

for i in $(seq 1 20); do
  if curl -sf "${BASE}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
log "server up at ${BASE}"

say "1. Ingest a memory (what we built today)"
curl -sf -X POST "${BASE}/ingest/memory" \
  -H "Content-Type: application/json" "${TOKEN_HEADER[@]}" \
  -d "$(cat <<JSON
{
  "user_id": "${USER_ID}",
  "title": "Parallax v0.6 Phase A",
  "summary": "Single parallax-server HTTP hub + multi-client shared kernel.",
  "vault_path": "notes/parallax-v06-phase-a.md"
}
JSON
)" | python -m json.tool

say "2. Ingest a claim (architectural decision)"
curl -sf -X POST "${BASE}/ingest/claim" \
  -H "Content-Type: application/json" "${TOKEN_HEADER[@]}" \
  -d "$(cat <<JSON
{
  "user_id": "${USER_ID}",
  "subject": "Parallax",
  "predicate": "is",
  "object": "content-addressed knowledge base with session continuity"
}
JSON
)" | python -m json.tool

say "3. Query by entity — L2 disclosure (title + evidence)"
curl -sf "${BASE}/query?kind=entity&user_id=${USER_ID}&q=Parallax&level=2" \
  "${TOKEN_HEADER[@]}" | python -m json.tool

say "4. Fetch the SessionStart reminder — this is what the next Claude session sees"
curl -sf "${BASE}/query/reminder?user_id=${USER_ID}" "${TOKEN_HEADER[@]}" \
  | python -c "import sys, json; print(json.load(sys.stdin)['reminder'])"

say "5. Inspect the instance"
curl -sf "${BASE}/inspect/info" "${TOKEN_HEADER[@]}" | python -m json.tool

say "Done. Session 不失憶."
