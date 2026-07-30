#!/usr/bin/env bash
# Vet — run the full three-act exam in the terminal (flags: python -m vet --help)
set -e
cd "$(dirname "$0")"

if [[ ! -x ./.venv/bin/python ]]; then
  cat >&2 <<'EOF'
Vet's Python environment is not installed yet.

Run:
  python3 -m venv .venv
  ./.venv/bin/python -m pip install -r requirements.txt

Then run ./run.sh again.
EOF
  exit 1
fi

export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-$(zsh -lic 'printf %s "$ANTHROPIC_API_KEY"' 2>/dev/null | tail -1)}"
exec ./.venv/bin/python -m vet "$@"
