# Operator Runbook

## Start each mode

1. **disabled**
   - Keep deployment mode set to `disabled`.
   - Run:
     ```bash
     python3 skills/btc-sprint-stack/scripts/run_startup_check.py \
       --deployment-mode path/to/deployment-mode.json \
       --prerequisites path/to/startup-prereqs.json
     ```
   - A healthy disabled startup returns `startup_ready` with `deployment_disabled`.

2. **dry_run**
   - Set deployment mode to `dry_run`.
   - Run startup verification:
     ```bash
     python3 skills/btc-sprint-stack/scripts/run_startup_check.py \
       --deployment-mode path/to/deployment-mode.json \
       --active-profile path/to/capped-safe-profile.json \
       --prerequisites path/to/startup-prereqs.json
     ```
   - Launch:
     ```bash
     python3 skills/btc-sprint-stack/scripts/run_active_mode.py \
       --deployment-mode path/to/deployment-mode.json \
       --active-profile path/to/capped-safe-profile.json \
       --operator-start-request path/to/start-request.json \
       --session-event-log path/to/session-events.jsonl \
       --runtime-event-log path/to/runtime-events.jsonl
     ```

3. **capped_live**
   - Keep the active profile on the last capped-safe profile.
   - Startup verification must return `startup_ready` or `startup_dry_run_only`.
   - Launch with `run_active_mode.py` only after the readiness probe succeeds.

4. **promoted_live**
   - Require a `promoted` decision from `apply_promotion_review.py`.
   - Set deployment mode to `promoted_live`.
   - Run startup verification before launch.
   - Launch with `run_active_mode.py` using the approved promoted profile.

5. **rolled_back**
   - Require a `rolled_back` decision from `apply_rollback.py`.
   - Set deployment mode to `rolled_back`.
   - Run startup verification before launch.
   - Launch with `run_active_mode.py` using the restored capped-safe profile.

## Run readiness probe

```bash
python3 skills/btc-sprint-stack/scripts/prove_live_ready.py \
  --config skills/btc-sprint-stack/config/live_probe.example.json \
  --event-log path/to/probe-events.jsonl
```

Use the probe before any live-capable startup. `ready_live` is required before routine capped or promoted live use.

## Run first live attempt

```bash
python3 skills/btc-sprint-stack/scripts/run_first_live_trade.py \
  --config skills/btc-sprint-stack/config/first_live_trade.example.json \
  --event-log path/to/first-live-events.jsonl
```

Run exactly one controlled attempt only after the readiness probe returns `ready_live`.

## Reconcile last trade

```bash
python3 skills/btc-sprint-stack/scripts/reconcile_last_trade.py \
  --payload path/to/reconciliation-payload.json \
  --event-log path/to/reconciliation-events.jsonl
```

Do not continue live use after `mismatch` or `unresolved`. Roll back or disable first.

## Read session report

```bash
python3 skills/btc-sprint-stack/scripts/report_last_session.py \
  --event-log path/to/session-events.jsonl
```

Review the final `clean`, `caution`, or `blocked` verdict before any scale-up decision.

## Apply promotion

1. Build the performance summary:
   ```bash
   python3 skills/btc-sprint-stack/scripts/evaluate_session_promotion.py \
     --session-report path/to/session-report-1.json \
     --session-report path/to/session-report-2.json
   ```
2. Apply explicit approval:
   ```bash
   python3 skills/btc-sprint-stack/scripts/apply_promotion_review.py \
     --promotion-summary path/to/performance-summary.json \
     --current-caps path/to/current-caps.json \
     --approval skills/btc-sprint-stack/config/promotion_profile.example.json \
     --output-profile path/to/promoted-profile.json \
     --event-log path/to/promotion-events.jsonl
   ```

Promotion is allowed only from `eligible_for_review` and with explicit approval.

## Apply rollback

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

Rollback restores the last capped-safe profile only.

## Stop conditions

Stop live operation immediately when any of the following occurs:

1. Readiness probe is not `ready_live`.
2. Reconciliation returns `mismatch` or `unresolved`.
3. Session verdict is `blocked`.
4. Performance summary is `blocked`.
5. Rollback decision is `rolled_back`.
6. Deployment mode becomes `disabled` or `blocked`.

## Safe operator actions

1. Switch to `dry_run` or `disabled` before investigating.
2. Re-run the readiness probe to confirm gate-chain health.
3. Reconcile the last trade before any further live activity.
4. Review the latest session report and performance summary.
5. Apply rollback when promoted behavior degrades or operator trust is lost.
6. Do not hand-edit promoted or rolled-back profiles to bypass the control plane.
