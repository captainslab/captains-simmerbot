from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.promotion_gate import session_caps_from_dict


ALLOWED_ACTIVE_MODES = {'disabled', 'dry_run', 'capped_live', 'promoted_live', 'rolled_back'}
APPROVED_ENTRYPOINTS = {
    'startup': ('skills/btc-sprint-stack/scripts/run_startup_check.py',),
    'smoke': ('skills/btc-sprint-stack/scripts/run_end_to_end_smoke.py',),
    'probe': ('skills/btc-sprint-stack/scripts/prove_live_ready.py',),
    'session_run': ('skills/btc-sprint-stack/scripts/run_active_mode.py',),
    'reconcile_report': (
        'skills/btc-sprint-stack/scripts/reconcile_last_trade.py',
        'skills/btc-sprint-stack/scripts/report_last_session.py',
    ),
    'rollback': ('skills/btc-sprint-stack/scripts/apply_rollback.py',),
}
DEPRECATED_ENTRYPOINTS = {
    'skills/btc-sprint-stack/scripts/run_live_session.py',
    'skills/btc-sprint-stack/scripts/run_first_live_trade.py',
    'skills/btc-sprint-stack/scripts/set_deployment_mode.py',
    'skills/btc-sprint-stack/scripts/apply_promotion_review.py',
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['FinalizeEvent'], event_type: str, **details: Any) -> None:
    events.append(
        FinalizeEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


@dataclass(frozen=True)
class OperatorState:
    startup_status: str
    smoke_verdict: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalizeEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FinalizeResult:
    status: str
    active_mode: str | None
    active_profile: dict[str, Any] | None
    entrypoints: dict[str, str] | None
    reasons: tuple[str, ...]
    events: tuple[FinalizeEvent, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


def operator_state_from_dict(payload: Mapping[str, Any]) -> OperatorState:
    return OperatorState(
        startup_status=str(payload['startup_status']),
        smoke_verdict=str(payload['smoke_verdict']),
        reasons=tuple(str(reason) for reason in payload.get('reasons', [])),
    )


def finalize_operator_path(
    *,
    operator_state: OperatorState | Mapping[str, Any],
    approved_mode: str,
    active_profile: Mapping[str, Any] | None,
    script_entrypoints: Mapping[str, str],
) -> FinalizeResult:
    resolved_state = operator_state if isinstance(operator_state, OperatorState) else operator_state_from_dict(operator_state)
    events: list[FinalizeEvent] = []
    _emit(
        events,
        'finalize_started',
        approved_mode=approved_mode,
        startup_status=resolved_state.startup_status,
        smoke_verdict=resolved_state.smoke_verdict,
    )

    if approved_mode not in ALLOWED_ACTIVE_MODES:
        return _finalize(
            events,
            status='blocked',
            active_mode=None,
            active_profile=None,
            entrypoints=None,
            reasons=(f'invalid_active_mode:{approved_mode}',),
        )
    if resolved_state.startup_status != 'startup_ready':
        return _finalize(
            events,
            status='blocked',
            active_mode=approved_mode,
            active_profile=_normalize_profile(active_profile),
            entrypoints=None,
            reasons=(f'operator_startup_not_ready:{resolved_state.startup_status}',),
        )
    if resolved_state.smoke_verdict != 'smoke_pass':
        return _finalize(
            events,
            status='blocked',
            active_mode=approved_mode,
            active_profile=_normalize_profile(active_profile),
            entrypoints=None,
            reasons=(f'operator_smoke_not_clean:{resolved_state.smoke_verdict}',),
        )

    normalized_profile = _normalize_profile(active_profile)
    profile_reason = _validate_profile(approved_mode, normalized_profile)
    if profile_reason is not None:
        return _finalize(
            events,
            status='blocked',
            active_mode=approved_mode,
            active_profile=normalized_profile,
            entrypoints=None,
            reasons=(profile_reason,),
        )

    entrypoint_reasons = _validate_entrypoints(script_entrypoints)
    if entrypoint_reasons:
        return _finalize(
            events,
            status='blocked',
            active_mode=approved_mode,
            active_profile=normalized_profile,
            entrypoints=None,
            reasons=tuple(entrypoint_reasons),
        )

    normalized_entrypoints = {key: _normalize_command(value) for key, value in script_entrypoints.items()}
    _emit(
        events,
        'active_path_locked',
        active_mode=approved_mode,
        active_profile=normalized_profile,
        entrypoints=normalized_entrypoints,
    )
    return _finalize(
        events,
        status='finalized',
        active_mode=approved_mode,
        active_profile=normalized_profile,
        entrypoints=normalized_entrypoints,
        reasons=('active_path_locked',),
    )


def _validate_profile(mode: str, profile: dict[str, Any] | None) -> str | None:
    if mode == 'disabled':
        return None
    if profile is None:
        return 'missing_active_profile'
    has_approval = all(key in profile for key in ('approval_id', 'approved_by', 'approved_at'))
    if mode == 'promoted_live' and not has_approval:
        return 'conflicting_profile_state'
    if mode in {'dry_run', 'capped_live', 'rolled_back'} and has_approval:
        return 'conflicting_profile_state'
    return None


def _validate_entrypoints(entrypoints: Mapping[str, str]) -> list[str]:
    reasons: list[str] = []
    missing_keys = [key for key in APPROVED_ENTRYPOINTS if key not in entrypoints]
    if missing_keys:
        reasons.extend(f'missing_entrypoint:{key}' for key in missing_keys)
        return reasons

    normalized = {key: _normalize_command(value) for key, value in entrypoints.items()}
    seen_commands: dict[str, str] = {}
    for key, command in normalized.items():
        if command in seen_commands:
            reasons.append(f'duplicate_entrypoint:{seen_commands[command]}:{key}')
        else:
            seen_commands[command] = key

    for key, scripts in APPROVED_ENTRYPOINTS.items():
        command = normalized[key]
        for deprecated in DEPRECATED_ENTRYPOINTS:
            if deprecated in command:
                reasons.append(f'deprecated_entrypoint:{key}:{deprecated}')
        for script in scripts:
            if script not in command:
                reasons.append(f'invalid_entrypoint:{key}')
                break
        if key != 'reconcile_report' and _count_python_scripts(command) != 1:
            reasons.append(f'duplicate_entrypoint:{key}')
        if key == 'reconcile_report' and _count_python_scripts(command) != 2:
            reasons.append('invalid_entrypoint:reconcile_report')
    return list(dict.fromkeys(reasons))


def _normalize_profile(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        caps = session_caps_from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None
    normalized = {
        'profile_name': str(payload.get('profile_name', 'session-profile')),
        'max_trades_per_session': caps.max_trades_per_session,
        'max_notional_per_session': caps.max_notional_per_session,
        'max_consecutive_losses': caps.max_consecutive_losses,
        'max_feed_age_seconds': caps.max_feed_age_seconds,
    }
    for optional_key in ('approval_id', 'approved_by', 'approved_at'):
        if payload.get(optional_key) is not None:
            normalized[optional_key] = payload[optional_key]
    return normalized


def _normalize_command(command: str) -> str:
    return ' '.join(str(command).split())


def _count_python_scripts(command: str) -> int:
    return len(re.findall(r'skills/btc-sprint-stack/scripts/[a-z0-9_]+\.py', command))


def _write_event_log(result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in result['events']:
            handle.write(json.dumps(event, sort_keys=True) + '\n')


def _finalize(
    events: list[FinalizeEvent],
    *,
    status: str,
    active_mode: str | None,
    active_profile: dict[str, Any] | None,
    entrypoints: dict[str, str] | None,
    reasons: tuple[str, ...],
) -> FinalizeResult:
    _emit(
        events,
        'active_path_verdict_emitted',
        status=status,
        active_mode=active_mode,
        reasons=list(reasons),
        active_profile=active_profile,
        entrypoints=entrypoints,
    )
    return FinalizeResult(
        status=status,
        active_mode=active_mode,
        active_profile=active_profile,
        entrypoints=entrypoints,
        reasons=reasons,
        events=tuple(events),
    )


def run_finalize_operator_path(
    *,
    operator_state_path: Path,
    active_mode_path: Path,
    active_profile_path: Path | None,
    output_state_path: Path | None = None,
    event_log_path: Path | None = None,
) -> dict[str, Any]:
    payload = json.loads(active_mode_path.read_text(encoding='utf-8'))
    operator_state = json.loads(operator_state_path.read_text(encoding='utf-8'))
    active_profile = None if active_profile_path is None else json.loads(active_profile_path.read_text(encoding='utf-8'))
    result = finalize_operator_path(
        operator_state=operator_state,
        approved_mode=str(payload['approved_mode']),
        active_profile=active_profile,
        script_entrypoints=dict(payload['script_entrypoints']),
    ).as_dict()
    if output_state_path is not None and result['status'] == 'finalized':
        output_state_path.parent.mkdir(parents=True, exist_ok=True)
        output_state_path.write_text(
            json.dumps(
                {
                    'active_mode': result['active_mode'],
                    'active_profile': result['active_profile'],
                    'entrypoints': result['entrypoints'],
                },
                sort_keys=True,
            ),
            encoding='utf-8',
        )
    if event_log_path is not None:
        _write_event_log(result, event_log_path)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Finalize and lock the approved BTC operator path')
    parser.add_argument('--operator-state', required=True, type=Path, help='Path to normalized operator state JSON')
    parser.add_argument('--active-mode', required=True, type=Path, help='Path to normalized active mode config JSON')
    parser.add_argument('--active-profile', type=Path, help='Path to normalized active profile JSON')
    parser.add_argument('--output-state', type=Path, help='Optional path to write finalized active path JSON')
    parser.add_argument('--event-log', type=Path, help='Optional append-only JSONL finalize event log path')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_finalize_operator_path(
        operator_state_path=args.operator_state,
        active_mode_path=args.active_mode,
        active_profile_path=args.active_profile,
        output_state_path=args.output_state,
        event_log_path=args.event_log,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
