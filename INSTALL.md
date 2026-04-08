# Installation

This repo is designed to be installed on any machine with Python 3.12+ and Git.

## 1. Clone the repo
```bash
git clone https://github.com/captainslab/captains-simmerbot.git
cd captains-simmerbot
```

## 2. Create a virtual environment
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 3. Create your local secrets file
```bash
mkdir -p "$HOME/.secrets"
cp .env.example "$HOME/.secrets/simmer-btc-sprint-bot.env"
```

Edit `$HOME/.secrets/simmer-btc-sprint-bot.env` and set the values you need:
- `SIMMER_API_KEY`
- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_API_KEY`
- `DISCORD_BOT_TOKEN`
- `DISCORD_WEBHOOK_URL`

If you store secrets elsewhere, set `BTC_SPRINT_SECRETS_FILE` before running the bot.
If you use a custom Python path, set `BTC_SPRINT_PYTHON_BIN` too.

## 4. Run a dry-run check
```bash
set -a
source "$HOME/.secrets/simmer-btc-sprint-bot.env"
set +a
./.venv/bin/python skills/btc-sprint-stack/main.py --once --dry-run --validate-real-path
```

## 5. Start the live loop
```bash
bin/start_btc_bot.sh
```

## Discord control
- Mention the bot or start a message with `?` for natural-language control.
- Use `!help` for shortcuts.
- Set `DISCORD_BOT_TOKEN` to enable the Discord bot and `DISCORD_WEBHOOK_URL` for alerts.
