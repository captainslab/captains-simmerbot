from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.startup_check import run_startup_check


def _write_event_log(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in result['events']:
            handle.write(json.dumps(event, sort_keys=True) + '\n')


def run_startup_check_from_files(
    *,
    deployment_mode_path: Path,
    active_profile_path: Path | None,
    prerequisites_path: Path,
    output_state_path: Path | None = None,
    event_log_path: Path | None = None,
) -> dict:
    deployment_mode = json.loads(deployment_mode_path.read_text(encoding='utf-8'))
    active_profile = None if active_profile_path is None else json.loads(active_profile_path.read_text(encoding='utf-8'))
    prerequisites = json.loads(prerequisites_path.read_text(encoding='utf-8'))
    result = run_startup_check(
        deployment_mode=deployment_mode,
        active_profile=active_profile,
        prerequisites=prerequisites,
    ).as_dict()
    if output_state_path is not None:
        output_state_path.parent.mkdir(parents=True, exist_ok=True)
        output_state_path.write_text(
            json.dumps(
                {
                    'status': result['status'],
                    'mode': result['mode'],
                    'profile': result['profile'],
                    'reasons': result['reasons'],
                },
                sort_keys=True,
            ),
            encoding='utf-8',
        )
    if event_log_path is not None:
        _write_event_log(result, event_log_path)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run normalized operator startup verification for the BTC sprint bot')
    parser.add_argument('--deployment-mode', required=True, type=Path, help='Path to normalized deployment-mode state JSON')
    parser.add_argument('--active-profile', type=Path, help='Path to normalized active profile JSON')
    parser.add_argument('--prerequisites', required=True, type=Path, help='Path to normalized startup prerequisites JSON')
    parser.add_argument('--output-state', type=Path, help='Optional path to write startup check result JSON')
    parser.add_argument('--event-log', type=Path, help='Optional append-only JSONL startup event log path')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_startup_check_from_files(
        deployment_mode_path=args.deployment_mode,
        active_profile_path=args.active_profile,
        prerequisites_path=args.prerequisites,
        output_state_path=args.output_state,
        event_log_path=args.event_log,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
