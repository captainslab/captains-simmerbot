from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.mode_launcher import launch_active_mode


def _write_event_log(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in result['events']:
            handle.write(json.dumps(event, sort_keys=True) + '\n')


def run_active_mode_from_files(
    *,
    deployment_mode_path: Path,
    active_profile_path: Path | None,
    operator_start_request_path: Path,
    session_event_log_path: Path | None = None,
    runtime_event_log_path: Path | None = None,
    output_state_path: Path | None = None,
) -> dict:
    deployment_mode = json.loads(deployment_mode_path.read_text(encoding='utf-8'))
    active_profile = None if active_profile_path is None else json.loads(active_profile_path.read_text(encoding='utf-8'))
    operator_start_request = json.loads(operator_start_request_path.read_text(encoding='utf-8'))
    result = launch_active_mode(
        deployment_mode=deployment_mode,
        active_profile=active_profile,
        operator_start_request=operator_start_request,
        session_event_log_path=session_event_log_path,
    ).as_dict()
    if runtime_event_log_path is not None:
        _write_event_log(result, runtime_event_log_path)
    if output_state_path is not None:
        output_state_path.parent.mkdir(parents=True, exist_ok=True)
        output_state_path.write_text(
            json.dumps(
                {
                    'status': result['status'],
                    'route': result['route'],
                    'profile': result['profile'],
                    'session_result': result['session_result'],
                },
                sort_keys=True,
            ),
            encoding='utf-8',
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Launch the BTC sprint bot in the active normalized deployment mode')
    parser.add_argument('--deployment-mode', required=True, type=Path, help='Path to normalized deployment-mode decision JSON')
    parser.add_argument('--active-profile', type=Path, help='Path to normalized active profile JSON')
    parser.add_argument('--operator-start-request', required=True, type=Path, help='Path to normalized operator start request JSON')
    parser.add_argument('--session-event-log', type=Path, help='Optional JSONL path for append-only session events')
    parser.add_argument('--runtime-event-log', type=Path, help='Optional JSONL path for append-only runtime launch events')
    parser.add_argument('--output-state', type=Path, help='Optional path to write runtime launch result JSON')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_active_mode_from_files(
        deployment_mode_path=args.deployment_mode,
        active_profile_path=args.active_profile,
        operator_start_request_path=args.operator_start_request,
        session_event_log_path=args.session_event_log,
        runtime_event_log_path=args.runtime_event_log,
        output_state_path=args.output_state,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
