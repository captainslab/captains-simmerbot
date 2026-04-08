# Autoresearch Configuration

## Goal
Improve the dry-run quality of `btc-sprint-stack` for BTC 5m/15m Polymarket sprint markets without changing bankroll controls or trade plumbing.

## Metric
- **Name**: accepted_candidates
- **Direction**: higher is better
- **Extract command**: `python3 - <<'PY'\nimport json,sys\nobj=json.load(sys.stdin)\nprint(obj['heartbeat']['accepted_candidates'])\nPY`

## Target Files
- `skills/btc-sprint-stack/config/defaults.json` (only tune the documented thresholds and cadence)
- `skills/btc-sprint-stack/data/skill_stack/registry.json` (tune signal skill weights and enabled flags)

## Read-Only Files
- `skills/btc-sprint-stack/main.py` (execution plumbing stays fixed)
- `skills/btc-sprint-stack/modules/` (signal, risk, journal, and executor logic stay fixed)
- `tests/test_btc_sprint_stack.py` (verification only)

## Run Command
```bash
cd "$HOME/apps/simmer-btc-sprint-bot"
set -a && source "$HOME/.secrets/simmer-btc-sprint-bot.env" && set +a
./.venv/bin/python skills/btc-sprint-stack/main.py --once --dry-run --validate-real-path
```

## Time Budget
- **Per experiment**: 5 minutes
- **Kill timeout**: 10 minutes

## Constraints
- Do not change bankroll or exposure caps.
- Tune only `min_edge`, `min_confidence`, `max_slippage_pct`, `cycle_interval_minutes`, `stop_loss_pct`, and `take_profit_pct`.
- Signal weights in registry.json must each be between 0.0 and 1.0.
- Stay in dry-run mode unless a human explicitly approves live trading.
- Prefer journal/backtest evidence before promoting any mutation to live usage.

## Installed Skills Available as Signal Sources
These ClawHub skills are installed and can be wired into the signal stack:
- `btc-analyzer` — BTC 15m candles, EMA20 + RSI14 direction signal
- `hourly-momentum-trader` — RSI, MACD, OBV, EMA, Bollinger Band confluence (-10 to +10 score)
- `market-sentiment-pulse` — crypto sentiment from news + social signals
- `polymarket-agent` — autonomous news research + market opportunity analysis
- `polymarket-auto-trader` — Kelly criterion position sizing + CLOB API
- `polymarket-odds` — live Polymarket odds query
- `polyedge` — cross-market correlation / mispriced market detection

## Branch
autoresearch/btc-sprint-stack

## Notes
- If multiple runs tie on `accepted_candidates`, prefer the run with fewer risk alerts and no execution errors.
- Use `skills/btc-sprint-stack/scripts/analyze_sprints.py` to review accumulated journal output before any live rollout.
