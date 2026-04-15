# Active Path

**Active default mode:** `dry_run`

## Startup command

```bash
python3 skills/btc-sprint-stack/scripts/run_startup_check.py \
  --deployment-mode path/to/deployment-mode.json \
  --active-profile path/to/active-profile.json \
  --prerequisites path/to/startup-prereqs.json
```

## Smoke command

```bash
python3 skills/btc-sprint-stack/scripts/run_end_to_end_smoke.py \
  --config skills/btc-sprint-stack/config/smoke_profile.example.json \
  --event-log path/to/smoke-events.jsonl
```

## Live-readiness probe command

```bash
python3 skills/btc-sprint-stack/scripts/prove_live_ready.py \
  --config skills/btc-sprint-stack/config/live_probe.example.json \
  --event-log path/to/probe-events.jsonl
```

## Session run command

```bash
python3 skills/btc-sprint-stack/scripts/run_active_mode.py \
  --deployment-mode path/to/deployment-mode.json \
  --active-profile path/to/active-profile.json \
  --operator-start-request path/to/start-request.json \
  --session-event-log path/to/session-events.jsonl \
  --runtime-event-log path/to/runtime-events.jsonl
```

## Reconcile/report flow

```bash
python3 skills/btc-sprint-stack/scripts/reconcile_last_trade.py \
  --payload path/to/reconciliation-payload.json \
  --event-log path/to/reconciliation-events.jsonl && \
python3 skills/btc-sprint-stack/scripts/report_last_session.py \
  --event-log path/to/session-events.jsonl
```

## Rollback path

```bash
python3 skills/btc-sprint-stack/scripts/apply_rollback.py \
  --promotion-state path/to/promotion-state.json \
  --current-profile path/to/current-profile.json \
  --prior-safe-profile path/to/capped-safe-profile.json \
  --performance-summary path/to/performance-summary.json \
  --trigger-config path/to/rollback-trigger.json \
  --output-profile path/to/rolled-back-profile.json \
  --event-log path/to/rollback-events.jsonl
```

## Safe operator notes

1. Routine use starts from `dry_run`; do not switch modes outside the normalized control path.
2. Do not use deprecated routine entrypoints such as `run_live_session.py`, `run_first_live_trade.py`, `set_deployment_mode.py`, or `apply_promotion_review.py` directly.
3. If startup, smoke, reconciliation, or reporting degrade, stop and return to `dry_run` or `disabled` before proceeding.
