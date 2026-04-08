#!/bin/bash
set -euo pipefail

set -a
source /home/jordan/.secrets/simmer-btc-sprint-bot.env
set +a
cd /home/jordan/apps/simmer-btc-sprint-bot
exec /home/jordan/apps/simmer-btc-sprint-bot/.venv/bin/python -u /home/jordan/apps/simmer-btc-sprint-bot/skills/btc-sprint-stack/main.py --loop --live --validate-real-path
