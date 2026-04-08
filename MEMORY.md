# Memory

## Current task
- BTC sprint bot is live and trading on Polymarket via Simmer.

## Resolved blockers
- LLM credentials: switched to OpenRouter free tier (`LLM_PROVIDER=openrouter`, `LLM_MODEL=openrouter/free`). Working as of 2026-04-07.
- Binance geo-block: `api.binance.com` returns 451 from DE server. Fixed by switching to `api.binance.us`.
- Discord webhook 403: fixed by adding `User-Agent` header to requests.
- Learned params drift: `live_params.json` and `pending_rules.json` reset to defaults on 2026-04-07 (729 stale rules cleared, `min_edge` restored to 0.07).

## Runtime constraints
- BTC only, Polymarket only.
- No `WALLET_PRIVATE_KEY` — using Simmer managed wallet.
- Non-root path only: `$HOME/apps/simmer-btc-sprint-bot`.
- Secrets file: `$HOME/.secrets/simmer-btc-sprint-bot.env`.

## Live run (2026-04-07)
- tmux session: `simmerbot`
- Start: `cd ~/apps/simmer-btc-sprint-bot && set -a && source ~/.secrets/simmer-btc-sprint-bot.env && set +a && .venv/bin/python skills/btc-sprint-stack/main.py --loop --live`
- Attach: `tmux attach -t simmerbot`
- Agent: `btc-sprint-stack-20260402` (id: dd01cb81-cbb8-4856-8e34-b385e2be9683)
- Wallet: managed, $64.16 USDC.e on Polygon (0x2829...c240)

## Secrets file keys required
```
SIMMER_API_KEY=sk_live_...
LLM_PROVIDER=openrouter
LLM_MODEL=openrouter/free
LLM_API_KEY=sk-or-v1-...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_IDS=...
DISCORD_CONTROL_CHANNEL_ID=...
DISCORD_CONTROL_PREFIX=simmer:
BINANCE_SYMBOL=BTCUSDT
BINANCE_INTERVAL=1m
TRADING_VENUE=polymarket
BTC_SPRINT_DRY_RUN=0
BTC_SPRINT_VALIDATE_REAL_PATH=1
```

## Persisted state files
- `skills/btc-sprint-stack/data/live_params.json`
- `skills/btc-sprint-stack/data/discord_control_state.json`
- `skills/btc-sprint-stack/data/pending_rules.json`
- `skills/btc-sprint-stack/data/llm_decisions.jsonl`

## Resume notes
- Keep the LLM prompt strict JSON only.
- Keep `max_trade_usd`, `max_daily_loss_usd`, `max_open_positions`, `max_single_market_exposure_usd`, `max_trades_per_day`, and slippage guardrails deterministic.
- Do not lower `min_edge` below 0.07 or `min_confidence` below 0.65 without trade history justifying it.
- `auto_redeem()` is called unconditionally each live cycle (works for both managed and external wallets).
- Binance endpoint: `api.binance.us` (not `.com` — geo-blocked from DE).
- Discord alerts require `User-Agent` header or Cloudflare returns 403.
- Discord chat control uses `DISCORD_ALLOWED_USER_IDS` allowlisting and natural-language parsing. It can update strategy labels, skill tags, and the live tunables exposed in `discord_control_state.json`, including trade sizing and other exposed risk knobs.
