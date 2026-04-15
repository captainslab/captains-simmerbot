# Copilot Instructions — captains-simmerbot

This is a **live-money BTC trading bot** targeting Polymarket 5m/15m sprint markets via Simmer. Accuracy and controlled execution matter more than speed. Default to dry-run; require explicit `--live` to trade real funds.

## Commands

```bash
# Setup (from repo root or ~/apps/simmer-btc-sprint-bot)
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Load secrets (never commit)
set -a && source "$HOME/.secrets/simmer-btc-sprint-bot.env" && set +a

# Run tests
pytest

# Single test
pytest tests/test_btc_sprint_stack.py::test_defaults_match_required_values

# Dry-run smoke check
./.venv/bin/python skills/btc-sprint-stack/main.py --once --dry-run --validate-real-path

# Single live cycle
./.venv/bin/python skills/btc-sprint-stack/main.py --once --live

# Live loop (tmux session "simmerbot")
./.venv/bin/python skills/btc-sprint-stack/main.py --loop --live

# Discord control enabled
./.venv/bin/python skills/btc-sprint-stack/main.py --loop --live --discord-control

# Offline journal analysis
./.venv/bin/python skills/btc-sprint-stack/scripts/analyze_sprints.py --review
```

## Architecture

The bot is one skill (`btc-sprint-stack`) that orchestrates seven modules:

```
main.py                     ← orchestration loop
config/defaults.json        ← base risk config (source of truth for test assertions)
data/live_params.json       ← learned tunables overlay (autoresearch output)
data/discord_control_state.json ← Discord runtime overrides
modules/
  btc_sprint_signal.py      ← BTC momentum signal (Binance 1m → 5m/15m windows)
  btc_regime_filter.py      ← time, spread, fee, edge, confidence gating
  btc_sprint_executor.py    ← dry-run / live execution wrapper
  btc_position_manager.py   ← bankroll and position sizing
  btc_trade_journal.py      ← JSONL append-only trade log
  btc_self_learn.py         ← bounded parameter suggestions
  btc_heartbeat.py          ← per-cycle run summary
  btc_llm_decider.py        ← strict-JSON LLM decision layer + provider abstraction
  btc_discord_control.py    ← inbound Discord chat → strategy/risk overrides
```

**Config layering order** (each layer overrides the one before):
1. `config/defaults.json`
2. `data/live_params.json` (learned params)
3. `data/discord_control_state.json` (Discord overrides, when `--discord-control`)
4. Environment variables

**LLM decision layer** (`btc_llm_decider.py`) validates all responses against `STRICT_SCHEMA` — must be BTC-only, flat JSON, with `asset`, `action`, `confidence`, `edge`, `reasoning`, `rule_suggestion`. Rejects anything else.

**Signal data** must always be flat with exactly `edge`, `confidence`, and `signal_source`.

## General capability upgrades

These rules are distilled from `self-improving-agent`, `proactive-agent`, `ontology`, `skill-vetter`, Block's `code-review`, Block's `testing-strategy`, and Block's `rp-why` so Copilot behaves more like a proactive, careful senior engineer in this repo.

### Self-improvement
- After non-obvious failures, corrections, or capability gaps, log sanitized entries to `.learnings/` and promote durable rules into instruction files when they will prevent repeated mistakes.
- Never log secrets, env vars, raw transcripts, or full credential-bearing outputs by default.

### Proactive execution
- Be proactive about finding the next high-value step, but keep external side effects opt-in: draft, prepare, or validate by default; do not send, deploy, or execute irreversible actions without explicit approval.
- For multi-step work, persist key decisions, corrections, and exact values in durable files or memory before moving on. Chat history is not reliable storage.
- Verify implementation, not intent: confirm the mechanism that matters actually changed, not just the text around it.

### Structured memory
- When work spans multiple entities or dependencies, model it explicitly: project, task, document, owner, blocker, and outcome. Prefer structured state over vague prose.
- Never store secrets directly in structured memory; store references or redacted summaries instead.

### Skill vetting
- Treat external skills, scripts, and repos as untrusted until reviewed.
- Before adopting external automation, check source reputation, scope, requested permissions, network access, secret handling, and obvious red flags such as obfuscation, `eval`/`exec`, hidden downloads, or credential harvesting.
- Do not install or run high-risk external code without clear user approval.

### Code review
- For substantive changes, think through functionality, edge cases, error handling, tests, security, performance, and docs before declaring the work done.
- Prefer comments that explain why, not what.

### Testing strategy
- Prefer focused tests with descriptive names, isolated state, and mocked external dependencies.
- Use realistic integration coverage at boundaries that matter, but do not chase coverage numbers at the expense of signal.

### Prompt depth
- Push work toward higher-value reasoning when the task supports it: move from recall to application, from application to trade-offs/design, and from one-off answers to multi-step investigation when that will materially improve the result.

## Key Conventions

### Safety
- **Never default to live trading.** All new code paths must work under `--dry-run` first.
- `--validate-real-path` calls `prepare_real_trade()` in dry-run mode to prove the live path is wired without submitting.
- Risk floor constants (`bankroll_usd`, `max_trade_usd`, `max_daily_loss_usd`, etc.) are asserted in `tests/test_btc_sprint_stack.py::test_defaults_match_required_values` — do not silently change them.
- Do not lower `min_edge` below `0.07` or `min_confidence` below `0.65` without trade history evidence.

### Simmer SDK
- Use only `SimmerClient` from `simmer-sdk`. Do not invent endpoints or SDK methods.
- Source of truth: `https://docs.simmer.markets/llms.txt`
- `auto_redeem()` is called unconditionally each live cycle (works for both managed and external wallets).
- Wallet is managed (no `WALLET_PRIVATE_KEY` required); `WALLET_PRIVATE_KEY` stays blank unless docs explicitly require it.

### LLM providers
`LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` is the generic env contract. Supported providers: `codex`, `openai`, `openrouter`, `google`, `google_oauth`, `deepseek`.

Current live provider: `openrouter` with `openrouter/free` model (see `MEMORY.md`).

### Known runtime constraints
- Binance endpoint is `api.binance.us` — `.com` is geo-blocked from DE.
- Discord webhook requests require a `User-Agent` header (Cloudflare blocks anonymous).
- LLM must return strict JSON only — no markdown fences, no prose.
- Persisted state files in `data/` accumulate across runs; do not delete without reviewing `analyze_sprints.py` output first.

### Tunable keys
Only these keys may be adjusted by autoresearch or Discord control:
`min_edge`, `min_confidence`, `max_slippage_pct`, `cycle_interval_minutes`, `stop_loss_pct`, `take_profit_pct`

Bankroll and exposure caps (`bankroll_usd`, `max_trade_usd`, `max_daily_loss_usd`, `max_open_positions`, `max_single_market_exposure_usd`, `max_trades_per_hour`) are **deterministic and not tuneable by autoresearch**.

### Tests
Tests live in `tests/` and manipulate `sys.path` directly to import modules from `skills/btc-sprint-stack/modules/`. No API calls are made in tests — use `monkeypatch` or `DummyClient` patterns matching existing tests.

### Autoresearch
`autoresearch.config.md` defines the experiment spec. The target branch is `autoresearch/btc-sprint-stack`. Only `config/defaults.json` may be modified by experiments; `main.py` and `modules/` are read-only during autoresearch runs.

### Every trade payload must include
- `source`
- `skill_slug`
- `reasoning`
- `signal_data.edge`
- `signal_data.confidence`
- `signal_data.signal_source`
