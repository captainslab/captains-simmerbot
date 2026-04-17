# Simmer BTC Sprint Bot

An automated BTC prediction bot that trades 5-minute Polymarket markets using momentum signals from Binance. Runs on the [Simmer](https://simmer.markets) agent platform with a dry-run-first safety model.

**What it does**
- Reads 1m BTCUSDT candlesticks from Binance to compute short-term momentum
- Finds open BTC Up/Down 5m markets on Polymarket
- Uses an LLM layer (Gemini, OpenAI, etc.) to validate the trade signal
- Executes live GTC limit orders via the Simmer SDK with a pre-submit guard
- Journals every decision, skip, and fill to `skills/btc-sprint-stack/data/journal.jsonl`

---

## Prerequisites

- Python 3.11+
- A [Simmer](https://simmer.markets) account with an agent created
- A Polymarket wallet (managed or self-custody — see below)
- An LLM API key or Google Cloud ADC credentials

---

## 1. Clone and install

```bash
git clone https://github.com/captainslab/captains-simmerbot.git
cd captains-simmerbot
python3 -m venv .venv
source .venv/bin/activate
pip install simmer-sdk pytest discord.py
```

---

## 2. Configure environment

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Minimum required fields in `.env`:

```env
SIMMER_API_KEY=your_simmer_api_key
SIMMER_AGENT_ID=your_agent_id
LLM_PROVIDER=google_oauth          # or google / openai / deepseek / openrouter
LLM_MODEL=gemini-2.5-flash
LLM_API_KEY=                        # leave blank if using google_oauth
```

---

## 3. Connect your Polymarket wallet

You have two options:

### Option A — Managed wallet (easiest, no private key needed)

Leave `WALLET_PRIVATE_KEY` blank in `.env`. Simmer holds a server-signed wallet for your agent. No extra setup required. Good for getting started.

### Option B — Self-custody wallet (recommended for real money)

Set your Polygon wallet's private key in `.env`:

```env
WALLET_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
```

On first startup the bot will:
1. Call `link_wallet` to associate your address with your Simmer agent
2. Call `set_approvals` to authorize Polymarket's CTF contract to use your USDC
3. Call `auto_redeem` each live cycle to claim resolved positions

Your private key is **never sent to Simmer** — it stays local and signs orders on your machine.

**To fund the wallet:**
- Deposit USDC on the Polygon network to your wallet address
- Polymarket markets settle in USDC on Polygon

### LLM provider setup

| Provider | How to authenticate |
|---|---|
| `google_oauth` | `gcloud auth application-default login` — no API key needed |
| `google` | Set `LLM_API_KEY` to a Gemini API key |
| `openai` | Set `LLM_API_KEY` to an OpenAI API key |
| `deepseek` | Set `LLM_API_KEY` to a DeepSeek API key |
| `openrouter` | Set `LLM_API_KEY` to an OpenRouter API key |

---

## 4. Run it

**Always start with a dry run:**

```bash
source .env  # or: set -a && source .env && set +a
.venv/bin/python skills/btc-sprint-stack/main.py --once --dry-run --validate-real-path
```

This runs one full cycle without submitting any orders. `--validate-real-path` calls `prepare_real_trade()` to prove the live path is wired.

**Single live trade:**

```bash
.venv/bin/python skills/btc-sprint-stack/main.py --once --live
```

**Continuous loop (every 15 minutes by default):**

```bash
.venv/bin/python skills/btc-sprint-stack/main.py --loop --live
```

**Stop the loop:**

```bash
pkill -f 'skills/btc-sprint-stack/main.py --loop'
```

---

## 5. Review results

```bash
.venv/bin/python skills/btc-sprint-stack/scripts/analyze_sprints.py --review
```

Outputs a summary of matched / failed / skipped trades, P&L, and signal quality from `journal.jsonl`.

---

## Discord control (optional)

The bot can receive natural-language strategy commands from a Discord channel.

Set in `.env`:
```env
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_ALLOWED_USER_IDS=123456789,987654321
DISCORD_CONTROL_CHANNEL_ID=your_channel_id
```

Run with control enabled:
```bash
.venv/bin/python skills/btc-sprint-stack/main.py --loop --live --discord-control
```

Example commands in Discord:
- `set min edge to 0.08`
- `set max trade to 6 dollars`
- `allow 3 open positions`
- `be more aggressive`
- `reset strategy`

---

## Risk defaults

Defaults live in `skills/btc-sprint-stack/config/defaults.json`. Key values:

| Setting | Default |
|---|---|
| Max trade size | $4 |
| Max daily loss | $10 |
| Max open positions | 2 |
| Min edge | 0.07 |
| Min confidence | 0.65 |
| Cycle interval | 15 min |

Set `BTC_SPRINT_PROFILE=aggressive` in `.env` to relax thresholds. The self-learning module (`btc_self_learn.py`) writes live threshold updates to `data/live_params.json` which override defaults at runtime.

---

## Repo layout

```
skills/btc-sprint-stack/
  main.py                  — entrypoint and main loop
  config/defaults.json     — risk defaults
  modules/
    btc_sprint_signal.py   — Binance momentum signal
    btc_regime_filter.py   — pre-trade regime gate
    btc_sprint_executor.py — order construction and submission
    btc_llm_decider.py     — LLM validation layer
    btc_position_manager.py
    btc_trade_journal.py
    btc_self_learn.py      — live threshold tuning
    btc_heartbeat.py
    btc_discord_control.py
    btc_discord_alert.py
  data/
    journal.jsonl          — trade log (gitignored)
    live_params.json       — runtime threshold overrides (gitignored)
  scripts/
    analyze_sprints.py     — offline P&L and signal analysis
tests/
  test_btc_sprint_executor.py
```

---

## Tests

```bash
.venv/bin/python -m pytest tests/test_btc_sprint_executor.py -v
```
