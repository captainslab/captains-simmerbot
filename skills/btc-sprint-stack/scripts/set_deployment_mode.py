from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.deployment_mode import evaluate_deployment_mode


def _write_event_log(decision: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in decision['events']:
            handle.write(json.dumps(event, sort_keys=True) + '\n')


def run_set_deployment_mode(
    *,
    operator_request_path: Path,
    profile_state_path: Path | None = None,
    promotion_state_path: Path | None = None,
    rollback_state_path: Path | None = None,
    output_state_path: Path | None = None,
    event_log_path: Path | None = None,
) -> dict:
    operator_request = json.loads(operator_request_path.read_text(encoding='utf-8'))
    profile_state = None if profile_state_path is None else json.loads(profile_state_path.read_text(encoding='utf-8'))
    promotion_state = None if promotion_state_path is None else json.loads(promotion_state_path.read_text(encoding='utf-8'))
    rollback_state = None if rollback_state_path is None else json.loads(rollback_state_path.read_text(encoding='utf-8'))
    decision = evaluate_deployment_mode(
        profile_state=profile_state,
        promotion_state=promotion_state,
        rollback_state=rollback_state,
        operator_request=operator_request,
    ).as_dict()
    if output_state_path is not None:
        output_state_path.parent.mkdir(parents=True, exist_ok=True)
        output_state_path.write_text(json.dumps({'mode': decision['mode'], 'profile': decision['profile']}, sort_keys=True), encoding='utf-8')
    if event_log_path is not None:
        _write_event_log(decision, event_log_path)
    return decision


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Set normalized deployment mode for the BTC sprint bot')
    parser.add_argument('--operator-request', required=True, type=Path, help='Path to normalized operator mode request JSON')
    parser.add_argument('--profile-state', type=Path, help='Path to normalized current profile JSON')
    parser.add_argument('--promotion-state', type=Path, help='Path to normalized promotion decision JSON')
    parser.add_argument('--rollback-state', type=Path, help='Path to normalized rollback decision JSON')
    parser.add_argument('--output-state', type=Path, help='Optional path to write deployment mode state JSON')
    parser.add_argument('--event-log', type=Path, help='Optional append-only JSONL deployment mode event log path')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_set_deployment_mode(
        operator_request_path=args.operator_request,
        profile_state_path=args.profile_state,
        promotion_state_path=args.promotion_state,
        rollback_state_path=args.rollback_state,
        output_state_path=args.output_state,
        event_log_path=args.event_log,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
