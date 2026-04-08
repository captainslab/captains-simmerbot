# Simmer BTC Sprint Bot

Dry-run-first BTC 5m/15m Polymarket sprint bot for Simmer.

## Current verified account state
- Simmer agent id: `dd01cb81-cbb8-4856-8e34-b385e2be9683`
- Agent status: `claimed`
- Real trading: `enabled`
- Wallet ownership: `native`
- Wallet credentials: `present`
- Wallet address: `0x2829...c240`
- LLM provider credentials: `present but invalid (provider returned 401 on 2026-04-05)`

## Repo layout
- `skills/btc-sprint-stack/` — primary skill
- `skills/simmer/` — installed Simmer SDK support skill from ClawHub
- `skills/autoresearch/` — installed autoresearch skill from ClawHub
- `skills/polymarket-fast-loop/` — BTC/ETH/SOL 5m/15m sprint trader (CEX momentum)
- `skills/polymarket-weather-trader/` — NOAA/Open-Meteo temperature market trader
- `skills/polymarket-copytrading/` — whale wallet mirror (polling + reactor modes)
- `skills/polymarket-ai-divergence/` — AI consensus vs market price divergence trader
- `skills/polymarket-mert-sniper/` — near-expiry conviction trading
- `skills/prediction-trade-journal/` — trade history sync, win rate, calibration reports
- `skills/polymarket-signal-sniper/` — RSS feed signal → market trade execution
- `skills/polymarket-elon-tweets/` — Elon tweet count bucket trading via XTracker
- `skills/simmer-x402/` — x402 micro-payment gateway for paid APIs (Kaito, AlphaKek, etc.)
- `skills/simmer-skill-builder/` — generate new Simmer skills from natural language strategy descriptions
- `skills/polymarket-wallet-xray/` — forensic wallet analysis (skill level, entry quality, bot detection)
- `skills/btc-sprint-stack/config/defaults.json` — exact risk defaults
- `skills/btc-sprint-stack/main.py` — entrypoint and loop
- `skills/btc-sprint-stack/modules/` — signal, filter, executor, PM, journal, self-learn, heartbeat, Discord control, LLM decision layer
- `skills/btc-sprint-stack/scripts/analyze_sprints.py` — offline journal analysis
- `skills/btc-sprint-stack/data/discord_control_state.json` — persisted Discord strategy overrides and skill tags
- `autoresearch.config.md` — day-one experiment configuration targeting safe threshold tuning only
- `MEMORY.md` — lightweight recovery notes and blockers

## Setup
```bash
cd "$HOME/apps/simmer-btc-sprint-bot"
python3 -m venv .venv
. .venv/bin/activate
pip install simmer-sdk pytest discord.py
```

Use the saved secret file without copying the key into the repo:
```bash
set -a
source "$HOME/.secrets/simmer-btc-sprint-bot.env"
set +a
```

For a more active live profile, set `BTC_SPRINT_PROFILE=aggressive` before running the loop or one-off live command. The default profile keeps the required risk floor from `AGENTS.md`.

The LLM layer now honors the documented generic env contract:
`LLM_PROVIDER`, `LLM_MODEL`, and `LLM_API_KEY`, with provider-specific fallbacks for OpenAI-compatible endpoints, including Google Gemini API keys.
The currently saved provider key was rotated to the Google API path for this lane, so the live bot now uses the Google-compatible OpenAI endpoint instead of OpenRouter.

## Discord control
The bot can listen to Discord chat and apply strategy updates from allowed users in a control channel. This is inbound control, not the webhook alert path.
It accepts natural-language instructions for strategy labels, skill tags, profile changes, and live risk knobs like trade size, open positions, daily loss, cooldown, and cycle timing.

Set these env vars before starting the bot:
- `DISCORD_BOT_TOKEN`
- `DISCORD_ALLOWED_USER_IDS`
- `DISCORD_CONTROL_CHANNEL_ID` (optional)
- `DISCORD_CONTROL_PREFIX` (optional legacy prefix; natural language works without it)

Run the bot with Discord control enabled:
```bash
cd "$HOME/apps/simmer-btc-sprint-bot"
set -a && source "$HOME/.secrets/simmer-btc-sprint-bot.env" && set +a
./.venv/bin/python skills/btc-sprint-stack/main.py --loop --live --discord-control
```

Examples:
- `be more aggressive`
- `set min edge to 0.08`
- `set max trade to 6 dollars`
- `allow 3 open positions`
- `set strategy label breakout`
- `add skill momentum`
- `reset strategy`

Discord chat updates the persisted control state in `skills/btc-sprint-stack/data/discord_control_state.json` and the next cycle loads those overrides.
Discord webhook alerts can mention configured allowed user IDs when you want the bot to ping you about a capability update.

## Dry-run smoke validation
```bash
cd "$HOME/apps/simmer-btc-sprint-bot"
set -a && source "$HOME/.secrets/simmer-btc-sprint-bot.env" && set +a
./.venv/bin/python skills/btc-sprint-stack/main.py --once --dry-run --validate-real-path
```

## Live command
```bash
cd "$HOME/apps/simmer-btc-sprint-bot"
set -a && source "$HOME/.secrets/simmer-btc-sprint-bot.env" && set +a
./.venv/bin/python skills/btc-sprint-stack/main.py --once --live
```

## Loop command
```bash
cd "$HOME/apps/simmer-btc-sprint-bot"
set -a && source "$HOME/.secrets/simmer-btc-sprint-bot.env" && set +a
./.venv/bin/python skills/btc-sprint-stack/main.py --loop --dry-run --validate-real-path
```

## Review command
```bash
cd "$HOME/apps/simmer-btc-sprint-bot"
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
