from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.rollback_gate import evaluate_rollback
from reporting.performance_summary import performance_summary_from_dict


def _write_event_log(decision: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in decision['events']:
            handle.write(json.dumps(event, sort_keys=True) + '\n')


def run_apply_rollback(
    *,
    promotion_state_path: Path,
    current_profile_path: Path,
    prior_safe_profile_path: Path | None,
    performance_summary_path: Path,
    trigger_config_path: Path,
    output_profile_path: Path | None = None,
    event_log_path: Path | None = None,
) -> dict:
    promotion_state = json.loads(promotion_state_path.read_text(encoding='utf-8'))
    current_profile = json.loads(current_profile_path.read_text(encoding='utf-8'))
    prior_safe_profile = None
    if prior_safe_profile_path is not None:
        prior_safe_profile = json.loads(prior_safe_profile_path.read_text(encoding='utf-8'))
    summary = performance_summary_from_dict(json.loads(performance_summary_path.read_text(encoding='utf-8')))
    trigger_config = json.loads(trigger_config_path.read_text(encoding='utf-8'))
    decision = evaluate_rollback(
        promotion_state=promotion_state,
        current_profile=current_profile,
        prior_safe_profile=prior_safe_profile,
        summary=summary,
        trigger_config=trigger_config,
    ).as_dict()
    if output_profile_path is not None and decision['status'] == 'rolled_back':
        output_profile_path.parent.mkdir(parents=True, exist_ok=True)
        output_profile_path.write_text(json.dumps(decision['profile'], sort_keys=True), encoding='utf-8')
    if event_log_path is not None:
        _write_event_log(decision, event_log_path)
    return decision


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Apply rollback to the last capped-safe profile when rollback triggers fire')
    parser.add_argument('--promotion-state', required=True, type=Path, help='Path to normalized promotion-state JSON')
    parser.add_argument('--current-profile', required=True, type=Path, help='Path to active profile JSON')
    parser.add_argument('--prior-safe-profile', type=Path, help='Path to last approved capped-safe profile JSON')
    parser.add_argument('--performance-summary', required=True, type=Path, help='Path to normalized performance-summary JSON')
    parser.add_argument('--trigger-config', required=True, type=Path, help='Path to rollback trigger config JSON')
    parser.add_argument('--output-profile', type=Path, help='Optional path to write rolled-back profile JSON')
    parser.add_argument('--event-log', type=Path, help='Optional append-only JSONL rollback event log path')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_apply_rollback(
        promotion_state_path=args.promotion_state,
        current_profile_path=args.current_profile,
        prior_safe_profile_path=args.prior_safe_profile,
        performance_summary_path=args.performance_summary,
        trigger_config_path=args.trigger_config,
        output_profile_path=args.output_profile,
        event_log_path=args.event_log,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
