# Polymarket BTC 15m Replay/Paper MVP Blueprint

## Objective
Build a modular, explainable, replay-first Polymarket BTC 15-minute system with strict no-live-funds constraints for phase 1.

## Exact repo structure (MVP additions)

```text
skills/btc-sprint-stack/
  config/
    defaults.json
    replay.paper.yaml                  # runtime + risk + adapter config
    schemas/
      config.schema.json               # JSON schema for runtime config
      event.schema.json                # append-only event schema
      round_decision.schema.json       # per-round explainability schema
  adapters/
    __init__.py
    contracts.py                       # adapter protocol/ABC definitions
    polymarket_gamma.py                # market metadata adapter
    polymarket_clob.py                 # orderbook/trade adapter
    btc_reference_price.py             # fallback reference price adapter
    health.py                          # staleness + heartbeat checks
  engine/
    __init__.py
    market_selector.py                 # active 15m market resolution
    feature_engine.py                  # prior + fast/slow/microstructure features
    decision_engine.py                 # weighted vote + penalties + gating
    risk_engine.py                     # sizing, caps, stop conditions
  brokers/
    __init__.py
    paper_broker.py                    # source-of-truth balance + fill simulation
    live_broker_stub.py                # interface only, no live calls in MVP
  telemetry/
    __init__.py
    event_log.py                       # append-only jsonl writer
    decision_log.py                    # round-level decision record writer
    pnl_ledger.py                      # realized/unrealized ledger
    alerts.py                          # parser/balance/stale/skipped-round alerts
  replay/
    __init__.py
    runner.py                          # historical windows end-to-end executor
    fixtures/
      malformed_timestamp.json
      feed_disconnect.json
      inverted_no_down_logic.json
      min_bet_order_ops.json
      concurrency_corruption.json
      duplicate_signal_confidence.json
  tests/
    test_market_selector.py
    test_data_health.py
    test_decision_explainability.py
    test_risk_min_bet_order.py
    test_replay_runner.py
    test_paper_broker_balance_truth.py
```

## Exact module boundaries
- `engine/market_selector.py`: resolve current BTC 15m market, validate close window, token mapping, and reject malformed/stale markets.
- `adapters/*`: all external API calls (Gamma/Data/CLOB/reference price) behind contracts; no direct requests from engine/broker modules.
- `engine/feature_engine.py`: deterministic feature extraction from normalized data snapshots only.
- `engine/decision_engine.py`: voting, de-duplication penalties, gate logic, and explainability payload assembly.
- `engine/risk_engine.py`: sizing and constraints only; no API access.
- `brokers/paper_broker.py`: idempotent order simulation, balance source-of-truth checks, fill/cancel/retry state machine.
- `telemetry/*`: write-only append logs + dashboard-ready aggregates.
- `replay/runner.py`: orchestrates adapters -> features -> decision -> risk -> broker -> telemetry for historical windows.

## Config schema (minimum keys)

```json
{
  "mode": "replay|paper",
  "clock": {"round_interval_sec": 900, "timestamp_tolerance_sec": 3},
  "adapters": {
    "gamma": {"base_url": "https://gamma-api.polymarket.com"},
    "clob": {"base_url": "https://clob.polymarket.com"},
    "btc_price": {"primary": "chainlink_streams", "fallback": "exchange_spot"},
    "health": {"max_staleness_sec": 20, "heartbeat_interval_sec": 5}
  },
  "decision": {
    "edge_threshold": 0.07,
    "min_confidence": 0.65,
    "correlation_penalty": 0.35,
    "synergy_bonus_cap": 0.1
  },
  "risk": {
    "bankroll_usd": 60,
    "max_trade_usd": 4,
    "max_daily_loss_usd": 10,
    "max_open_positions": 2,
    "max_single_market_exposure_usd": 8,
    "max_trades_per_day": 6,
    "max_slippage_pct": 0.1,
    "stop_loss_pct": 0.1,
    "take_profit_pct": 0.12,
    "cooldown_after_loss_minutes": 60
  },
  "telemetry": {
    "event_log_path": "skills/btc-sprint-stack/data/events.jsonl",
    "decision_log_path": "skills/btc-sprint-stack/data/round_decisions.jsonl",
    "pnl_log_path": "skills/btc-sprint-stack/data/pnl_ledger.jsonl"
  }
}
```

## Event schema (append-only)

```json
{
  "event_id": "uuid",
  "ts_utc": "RFC3339",
  "run_id": "uuid",
  "round_id": "YYYYMMDD-HHMM",
  "event_type": "adapter_health|market_selected|decision|order_submitted|order_filled|order_canceled|alert",
  "source": "module_path",
  "severity": "info|warn|error",
  "payload": {}
}
```

## Round decision schema

```json
{
  "run_id": "uuid",
  "round_id": "YYYYMMDD-HHMM",
  "market": {
    "market_id": "string",
    "condition_id": "string",
    "yes_token_id": "string",
    "no_token_id": "string",
    "close_time_utc": "RFC3339"
  },
  "inputs": {
    "timestamps": {"local_received": "RFC3339", "normalized": "RFC3339"},
    "price_snapshot": {},
    "orderbook_snapshot": {},
    "trade_snapshot": {},
    "adapter_health": {}
  },
  "votes": [
    {
      "name": "signal_name",
      "family": "fast|slow|structure|memory|correlated",
      "raw_vote": 0.0,
      "weight": 0.0,
      "penalties": {"duplication": 0.0, "correlation": 0.0},
      "adjusted_vote": 0.0
    }
  ],
  "aggregate": {
    "prior": 0.0,
    "weighted_vote": 0.0,
    "synergy_bonus": 0.0,
    "edge": 0.0,
    "confidence": 0.0
  },
  "gates": {
    "data_fresh": true,
    "market_valid": true,
    "risk_ok": true,
    "min_edge_ok": true,
    "min_confidence_ok": true,
    "result": "trade|no_trade"
  },
  "sizing": {
    "sizing_model": "bounded_kelly",
    "kelly_fraction_raw": 0.0,
    "kelly_fraction_capped": 0.0,
    "notional_usd": 0.0,
    "min_bet_applied": false,
    "cap_reasons": []
  },
  "final_action": {
    "action": "buy_yes|buy_no|hold",
    "reason": "string",
    "broker_mode": "replay|paper"
  }
}
```

## Adapter contract definitions (required)
- `MarketMetadataAdapter`: `list_open_markets()`, `get_market(market_id)`, `healthcheck()`.
- `OrderbookTradeAdapter`: `get_orderbook(token_id)`, `get_recent_trades(token_id, limit)`, `healthcheck()`.
- `ReferencePriceAdapter`: `get_spot(symbol)`, `get_candle(symbol, interval)`, `healthcheck()`.
- `Broker`: `sync_balance()`, `place_order(intent)`, `cancel(order_id)`, `poll(order_id)`, `healthcheck()`.

## Replay-first MVP plan
1. Implement adapter contracts + timestamp normalization utility.
2. Implement market selector with strict 15-minute window validation and explicit malformed timestamp errors.
3. Implement replay runner with fixture-driven adapter inputs.
4. Implement decision schema writer and event log writer.
5. Implement risk engine min-bet order-of-operations tests.
6. Implement paper broker with verified balance state machine (no synthetic balance minting).
7. Wire no-trade gates for stale/disconnected feeds and failed health checks.
8. Execute fixture suite and export proof bundle artifacts.

## Proof checklist (MVP done criteria)
- [ ] Representative BTC 15m market discovery pass/fail logs archived.
- [ ] Malformed timestamp fixture raises explicit typed error and emits alert event.
- [ ] Feed disconnect fixture forces no-trade decision with gate evidence.
- [ ] Round decision records include inputs, weights, penalties, gates, size, action.
- [ ] Replay runner processes historical windows end-to-end.
- [ ] Paper broker settlement uses source-of-truth balance snapshots.
- [ ] Required failure-mode tests are present and passing.

## One exact next implementation step
Create `skills/btc-sprint-stack/adapters/contracts.py` with typed `Protocol` interfaces and unit tests that fail if engine modules import external clients directly.
