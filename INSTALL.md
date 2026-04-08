# Installation

This guide gets the bot running on a new machine with the least friction.

## Prerequisites

- Python 3.12 or newer
- Git
- A Simmer API key
- A Discord bot token if you want the conversational bot
- A Discord webhook URL if you want alerts

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

Edit `$HOME/.secrets/simmer-btc-sprint-bot.env` and set:

- `SIMMER_API_KEY`
- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_API_KEY`
- `DISCORD_BOT_TOKEN`
- `DISCORD_WEBHOOK_URL`

If you keep secrets elsewhere, set `BTC_SPRINT_SECRETS_FILE` to that file.
If you use a custom Python binary, set `BTC_SPRINT_PYTHON_BIN`.

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

## 6. Talk to it in Discord

- Mention the bot, or start a message with `?`, for natural-language control.
- Use `!help` for shortcuts.
- Use `!status`, `!cycle`, `!markets`, `!chart`, and `!briefing` when you want direct commands.

## 7. Update later

See [UPDATES.md](UPDATES.md) for the clean update flow.
