# Simmer BTC Sprint Bot

Dry-run-first BTC 5m/15m Polymarket sprint bot for Simmer.

## Repo layout
- `skills/btc-sprint-stack/` — primary skill
- `skills/simmer/` — installed Simmer SDK support skill from ClawHub
- `skills/autoresearch/` — installed autoresearch skill from ClawHub
- `skills/btc-sprint-stack/config/defaults.json` — exact risk defaults
- `skills/btc-sprint-stack/main.py` — entrypoint and loop
- `skills/btc-sprint-stack/modules/` — signal, filter, executor, PM, journal, self-learn, heartbeat, LLM decision layer
- `skills/btc-sprint-stack/scripts/analyze_sprints.py` — offline journal analysis
- `autoresearch.config.md` — day-one experiment configuration targeting safe threshold tuning only
- `MEMORY.md` — lightweight recovery notes and blockers
- `INSTALL.md` — quick install and launch guide for other machines

## Install
Clone the repo on any machine, then set up a local virtualenv:
```bash
git clone https://github.com/captainslab/captains-simmerbot.git
cd captains-simmerbot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Create your local secret file without copying secrets into the repo:
```bash
cp .env.example "$HOME/.secrets/simmer-btc-sprint-bot.env"
set -a
source "$HOME/.secrets/simmer-btc-sprint-bot.env"
set +a
```

Set at least:
- `SIMMER_API_KEY`
- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_API_KEY`
- `DISCORD_BOT_TOKEN` and `DISCORD_WEBHOOK_URL` if you want Discord control and alerts

If you keep your secrets somewhere else, point `BTC_SPRINT_SECRETS_FILE` at that file before launching.
If you use a non-default Python location, point `BTC_SPRINT_PYTHON_BIN` at that binary before launching.

For a more active live profile, set `BTC_SPRINT_PROFILE=aggressive` before running the loop or one-off live command. The default profile keeps the required risk floor from `AGENTS.md`.

The LLM layer now honors the documented generic env contract:
`LLM_PROVIDER`, `LLM_MODEL`, and `LLM_API_KEY`, with provider-specific fallbacks for OpenAI-compatible endpoints, including Google Gemini API keys.
The currently saved provider key was rotated to the Google API path for this lane, so the live bot now uses the Google-compatible OpenAI endpoint instead of OpenRouter.

## Discord control
The bot now supports natural-language control in Discord first:
- Mention the bot, or start a message with `?`, and ask for what you want in plain English.
- Use `!` shortcuts only as a fallback for direct commands like `!status`, `!cycle`, `!markets`, or `!help`.
- Set `DISCORD_BOT_TOKEN` to enable the conversational bot, and `DISCORD_WEBHOOK_URL` to enable alerts.
- You can override control-plane paths with `BTC_SPRINT_SECRETS_FILE`, `BTC_SPRINT_APPS_ROOT`, `BTC_SPRINT_SKILL_LIBRARY`, `BTC_SPRINT_TMUX_SESSION`, and `BTC_SPRINT_TMUX_MAIN_WIN`.

## Dry-run smoke validation
```bash
cd "$(git rev-parse --show-toplevel)"
set -a && source "$HOME/.secrets/simmer-btc-sprint-bot.env" && set +a
./.venv/bin/python skills/btc-sprint-stack/main.py --once --dry-run --validate-real-path
```

## Live command
```bash
cd "$(git rev-parse --show-toplevel)"
set -a && source "$HOME/.secrets/simmer-btc-sprint-bot.env" && set +a
./.venv/bin/python skills/btc-sprint-stack/main.py --once --live
```

## Loop command
```bash
cd "$(git rev-parse --show-toplevel)"
set -a && source "$HOME/.secrets/simmer-btc-sprint-bot.env" && set +a
./.venv/bin/python skills/btc-sprint-stack/main.py --loop --dry-run --validate-real-path
```

For live Discord startup, the tracked helper script is:
```bash
bin/start_btc_bot.sh
```

## Review command
```bash
cd "$(git rev-parse --show-toplevel)"
./.venv/bin/python skills/btc-sprint-stack/scripts/analyze_sprints.py --review
```

## Stop command
```bash
pkill -f 'skills/btc-sprint-stack/main.py --loop'
```

## Autoresearch workflow
```bash
cd "$HOME/apps/simmer-btc-sprint-bot"
npx clawhub@latest --workdir "$HOME/apps/simmer-btc-sprint-bot" list
# autoresearch is installed as a skill, not a plugin package
# use autoresearch.config.md as the guardrailed experiment spec
```

## Notes
- Dry-run never submits a trade. It can optionally call `prepare_real_trade()` to prove the live path is wired.
- Live mode is explicit with `--live`.
- Signal data is flat and always includes `edge`, `confidence`, and `signal_source`.
- The bot uses the official `SimmerClient` from `simmer-sdk`.
