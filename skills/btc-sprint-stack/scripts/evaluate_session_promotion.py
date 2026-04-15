from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reporting.performance_summary import build_performance_summary, load_session_reports


def run_session_promotion_evaluation(report_paths: list[Path]) -> dict:
    summary = build_performance_summary(load_session_reports(report_paths))
    return summary.as_dict()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Evaluate whether session history is eligible for scale-up review')
    parser.add_argument(
        '--session-report',
        required=True,
        action='append',
        dest='session_reports',
        type=Path,
        help='Path to a normalized session report JSON file; repeat for multiple sessions',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(json.dumps(run_session_promotion_evaluation(args.session_reports), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
