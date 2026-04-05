# Memory

## Current task
- Upgrade `btc-sprint-stack` into an LLM-assisted trader with deterministic risk as the final gate.

## Verified blockers
- Configured LLM provider credentials are present but currently invalid.
- Direct provider smoke call on `2026-04-05` returned `401 Unauthorized` with `invalid_request_error`, so the outbound LLM path is still blocked until the key is rotated or corrected.

## Runtime constraints
- BTC only.
- Polymarket only.
- No `WALLET_PRIVATE_KEY`.
- Non-root path only: `$HOME/apps/simmer-btc-sprint-bot`.
- Existing secrets file only: `$HOME/.secrets/simmer-btc-sprint-bot.env`.

## Persisted state files
- `skills/btc-sprint-stack/data/live_params.json`
- `skills/btc-sprint-stack/data/pending_rules.json`
- `skills/btc-sprint-stack/data/llm_decisions.jsonl`

## Resume notes
- Keep the LLM prompt strict JSON only.
- Keep `max_trade_usd`, `max_daily_loss_usd`, `max_open_positions`, `max_single_market_exposure_usd`, `max_trades_per_day`, and slippage guardrails deterministic.
- The generic env contract is now aligned in code: `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, and provider-specific fallbacks work.
