from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.reconciliation import (
    ReconciliationResult,
    balance_snapshot_from_dict,
    broker_order_from_dict,
    order_intent_from_dict,
    reconcile_trade,
)


def _write_events(result: ReconciliationResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in result.events:
            handle.write(json.dumps({'event_type': event.event_type, 'timestamp': event.timestamp, 'details': event.details}, sort_keys=True) + '\n')


def run_reconcile_last_trade(payload_path: Path, *, event_log_path: Path | None = None) -> ReconciliationResult:
    if not payload_path.exists():
        result = ReconciliationResult(
            status='unresolved',
            reasons=('missing_reconciliation_payload',),
            balance_delta=0.0,
            events=(),
        )
        terminal = ReconciliationResult(
            status=result.status,
            reasons=result.reasons,
            balance_delta=result.balance_delta,
            events=result.events,
        )
        if event_log_path is not None:
            _write_events(terminal, event_log_path)
        return terminal

    payload = json.loads(payload_path.read_text())
    try:
        intent = order_intent_from_dict(payload['order_intent'])
        broker_order = broker_order_from_dict(payload['broker_order']) if payload.get('broker_order') is not None else None
        balance_before = balance_snapshot_from_dict(payload['balance_before'])
        balance_after = balance_snapshot_from_dict(payload['balance_after'])
    except KeyError as exc:
        result = ReconciliationResult(
            status='unresolved',
            reasons=(f'missing_payload_field:{exc.args[0]}',),
            balance_delta=0.0,
            events=(),
        )
        if event_log_path is not None:
            _write_events(result, event_log_path)
        return result

    result = reconcile_trade(
        intent=intent,
        broker_order=broker_order,
        balance_before=balance_before,
        balance_after=balance_after,
    )
    if event_log_path is not None:
        _write_events(result, event_log_path)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Reconcile the last normalized trade attempt')
    parser.add_argument('--payload', required=True, type=Path, help='Path to normalized reconciliation payload JSON')
    parser.add_argument('--event-log', type=Path, help='Optional JSONL path for append-only reconciliation events')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_reconcile_last_trade(args.payload, event_log_path=args.event_log)
    print(result.status)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
