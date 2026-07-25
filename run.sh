#!/usr/bin/env bash
# Vet — launch the demo server on http://localhost:8787
set -e
cd "$(dirname "$0")"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$(zsh -lic 'printf %s "$ANTHROPIC_API_KEY"' 2>/dev/null | tail -1)}"
exec ./.venv/bin/uvicorn vet.server:app --host 127.0.0.1 --port 8787 "$@"
