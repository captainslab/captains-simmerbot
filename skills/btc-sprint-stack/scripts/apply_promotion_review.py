from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.promotion_gate import evaluate_promotion_review
from reporting.performance_summary import performance_summary_from_dict


def _write_event_log(decision: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in decision['events']:
            handle.write(json.dumps(event, sort_keys=True) + '\n')


def run_apply_promotion_review(
    *,
    promotion_summary_path: Path,
    current_caps_path: Path,
    approval_path: Path | None = None,
    output_profile_path: Path | None = None,
    event_log_path: Path | None = None,
) -> dict:
    summary = performance_summary_from_dict(json.loads(promotion_summary_path.read_text(encoding='utf-8')))
    current_caps = json.loads(current_caps_path.read_text(encoding='utf-8'))
    approval = None if approval_path is None else json.loads(approval_path.read_text(encoding='utf-8'))
    decision = evaluate_promotion_review(
        summary=summary,
        current_caps=current_caps,
        approval=approval,
    ).as_dict()
    if output_profile_path is not None and decision['status'] == 'promoted':
        output_profile_path.parent.mkdir(parents=True, exist_ok=True)
        output_profile_path.write_text(json.dumps(decision['profile'], sort_keys=True), encoding='utf-8')
    if event_log_path is not None:
        _write_event_log(decision, event_log_path)
    return decision


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Apply human-approved promotion review to current session caps')
    parser.add_argument('--promotion-summary', required=True, type=Path, help='Path to normalized promotion summary JSON')
    parser.add_argument('--current-caps', required=True, type=Path, help='Path to current capped session profile JSON')
    parser.add_argument('--approval', type=Path, help='Path to explicit promotion approval artifact JSON')
    parser.add_argument('--output-profile', type=Path, help='Optional path to write promoted profile JSON')
    parser.add_argument('--event-log', type=Path, help='Optional append-only JSONL promotion event log path')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_apply_promotion_review(
        promotion_summary_path=args.promotion_summary,
        current_caps_path=args.current_caps,
        approval_path=args.approval,
        output_profile_path=args.output_profile,
        event_log_path=args.event_log,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
