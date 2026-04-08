# Simmer BTC Sprint Bot

> BTC-only. Dry-run-first. Discord-native. Built to be cloned, configured, and improved by anyone.

This repo is a live trading stack for BTC 5m/15m Polymarket sprint markets on Simmer. It scans live markets, applies deterministic risk gates, writes every decision to a journal, and lets you control the bot in plain English from Discord.

## What it does

| Capability | What it gives you |
| --- | --- |
| Natural-language Discord control | Mention the bot or start a message with `?` and ask what you want in plain English. |
| BTC market scanning | Finds BTC fast markets and surfaces what looks interesting right now. |
| Deterministic risk gating | Enforces bankroll, slippage, position, and trade-count limits before execution. |
| Trade journaling | Stores every cycle and result so you can review, chart, and export it later. |
| Self-learning tunables | Lets the bot propose and apply bounded parameter updates over time. |
| Alerts and briefings | Sends morning summaries, alerts, and trade updates to Discord. |

## The flow

```text
Discord message
  -> LLM interpretation
  -> market scan
  -> regime filter
  -> bankroll/risk gate
  -> executor
  -> journal + heartbeat + alerts
```

## Quick start

1. Read the install guide: [INSTALL.md](INSTALL.md)
2. Copy the example env file and set your own credentials.
3. Start the bot with [bin/start_btc_bot.sh](bin/start_btc_bot.sh).

## How to talk to it

Natural-language examples:

- "What looks best right now?"
- "Run a cycle"
- "Why did you skip that trade?"
- "Give me a morning briefing"
- "Show the last 20 trades"

Shortcut commands still work when you want a direct action:

| Command | Use |
| --- | --- |
| `!help` | Show the command list |
| `!status` | Show current performance and risk state |
| `!cycle` | Trigger one trading cycle |
| `!markets` | Scan live BTC markets |
| `!chart` | Show an ASCII PnL chart |
| `!export` | Export recent trades as CSV |
| `!briefing` | Generate a morning briefing |
| `!logs` | Tail a tmux log window |
| `!restart` | Restart the main bot process |
| `!stopall` | Stop running skills |
| `!alert` | Set a BTC price or win-rate alert |
| `!skill ...` | List, install, or stop skills |

## Configuration

Set these in your local secrets file:

| Variable | Required | Purpose |
| --- | --- | --- |
| `SIMMER_API_KEY` | Yes | Authenticates against Simmer |
| `LLM_PROVIDER` | Yes | Chooses the model backend |
| `LLM_MODEL` | Yes | Sets the model name |
| `LLM_API_KEY` | Yes | Provider credential |
| `DISCORD_BOT_TOKEN` | Optional | Enables conversational Discord control |
| `DISCORD_WEBHOOK_URL` | Optional | Enables Discord alerts |
| `BTC_SPRINT_SECRETS_FILE` | Optional | Custom secrets file path |
| `BTC_SPRINT_PYTHON_BIN` | Optional | Custom Python binary path |
| `BTC_SPRINT_APPS_ROOT` | Optional | Custom apps root for tmux launches |
| `BTC_SPRINT_SKILL_LIBRARY` | Optional | Custom skill library path |
| `BTC_SPRINT_TMUX_SESSION` | Optional | Custom tmux session name |
| `BTC_SPRINT_TMUX_MAIN_WIN` | Optional | Custom tmux main window name |

## Install and run

For the full setup flow, use [INSTALL.md](INSTALL.md).

Typical first-time flow:

```bash
git clone https://github.com/captainslab/captains-simmerbot.git
cd captains-simmerbot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example "$HOME/.secrets/simmer-btc-sprint-bot.env"
bin/start_btc_bot.sh
```

## Keep it current

When you want the latest changes:

1. Pull the repo.
2. Reinstall dependencies if `requirements.txt` changed.
3. Re-run the tests.
4. Restart the loop.

The full update flow lives in [UPDATES.md](UPDATES.md).

## Repo map

| Path | Purpose |
| --- | --- |
| `skills/btc-sprint-stack/main.py` | Main loop and trade orchestration |
| `skills/btc-sprint-stack/modules/btc_discord_bot.py` | Discord conversation and commands |
| `skills/btc-sprint-stack/modules/btc_llm_decider.py` | Strict JSON LLM gate |
| `skills/btc-sprint-stack/modules/btc_heartbeat.py` | Briefings and cycle summaries |
| `skills/btc-sprint-stack/modules/btc_trade_journal.py` | Trade journal writer/reader |
| `skills/btc-sprint-stack/scripts/analyze_sprints.py` | Offline review of the journal |
| `INSTALL.md` | Step-by-step install guide |
| `UPDATES.md` | How to pull and ship updates safely |

## Safety model

- Default to dry-run.
- Keep BTC-only scope.
- Keep risk gates deterministic.
- Never commit secrets.
- Update the tunable values in the secret file, not the code, unless you are changing behavior intentionally.

## Contributing

If you improve the bot:

1. Make the change.
2. Run the tests.
3. Update the docs if behavior changed.
4. Commit and push.

That keeps the repo easy for other people to install and trust.
