#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_FILE="${BTC_SPRINT_SECRETS_FILE:-$HOME/.secrets/simmer-btc-sprint-bot.env}"
PYTHON_BIN="${BTC_SPRINT_PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"

if [[ ! -f "$SECRETS_FILE" ]]; then
  echo "Missing secrets file: $SECRETS_FILE" >&2
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing Python executable: $PYTHON_BIN" >&2
  exit 1
fi

set -a
source "$SECRETS_FILE"
set +a
cd "$REPO_ROOT"
exec "$PYTHON_BIN" -u "$REPO_ROOT/skills/btc-sprint-stack/main.py" --loop --live --validate-real-path
