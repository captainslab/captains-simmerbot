from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reporting.session_report import build_session_report, load_session_events


def run_report_last_session(event_log_path: Path) -> dict:
    report = build_session_report(load_session_events(event_log_path))
    return report.as_dict()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Summarize the last normalized live session')
    parser.add_argument('--event-log', required=True, type=Path, help='Path to append-only session JSONL log')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(json.dumps(run_report_last_session(args.event_log), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
