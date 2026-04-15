from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from execution.promotion_gate import session_caps_from_dict
from runtime.mode_launcher import ALLOWED_RUNTIME_MODES, DeploymentModeState, deployment_mode_state_from_dict


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['StartupCheckEvent'], event_type: str, **details: Any) -> None:
    events.append(
        StartupCheckEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


@dataclass(frozen=True)
class StartupPrerequisites:
    runtime_launcher_script_path: str
    readiness_probe_script_path: str
    readiness_probe_config_path: str
    first_live_trade_script_path: str
    reconcile_last_trade_script_path: str
    session_report_script_path: str
    session_event_log_path: str
    session_report_output_path: str
    reconciliation_payload_path: str


@dataclass(frozen=True)
class StartupCheckEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StartupCheckResult:
    status: str
    mode: str
    profile: dict[str, Any] | None
    reasons: tuple[str, ...]
    events: tuple[StartupCheckEvent, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


def startup_prerequisites_from_dict(payload: Mapping[str, Any]) -> StartupPrerequisites:
    return StartupPrerequisites(
        runtime_launcher_script_path=str(payload['runtime_launcher_script_path']),
        readiness_probe_script_path=str(payload['readiness_probe_script_path']),
        readiness_probe_config_path=str(payload['readiness_probe_config_path']),
        first_live_trade_script_path=str(payload['first_live_trade_script_path']),
        reconcile_last_trade_script_path=str(payload['reconcile_last_trade_script_path']),
        session_report_script_path=str(payload['session_report_script_path']),
        session_event_log_path=str(payload['session_event_log_path']),
        session_report_output_path=str(payload['session_report_output_path']),
        reconciliation_payload_path=str(payload['reconciliation_payload_path']),
    )


def run_startup_check(
    *,
    deployment_mode: DeploymentModeState | Mapping[str, Any],
    active_profile: Mapping[str, Any] | None,
    prerequisites: StartupPrerequisites | Mapping[str, Any],
) -> StartupCheckResult:
    resolved_mode = (
        deployment_mode if isinstance(deployment_mode, DeploymentModeState) else deployment_mode_state_from_dict(deployment_mode)
    )
    try:
        resolved_prereqs = (
            prerequisites
            if isinstance(prerequisites, StartupPrerequisites)
            else startup_prerequisites_from_dict(prerequisites)
        )
    except (KeyError, TypeError, ValueError):
        return StartupCheckResult(
            status='startup_blocked',
            mode='blocked',
            profile=None,
            reasons=('invalid_startup_prerequisites',),
            events=(
                StartupCheckEvent(
                    event_type='startup_verdict_emitted',
                    timestamp=_utc_now(),
                    details={'status': 'startup_blocked', 'reasons': ['invalid_startup_prerequisites'], 'mode': 'blocked'},
                ),
            ),
        )

    events: list[StartupCheckEvent] = []
    normalized_profile = _normalize_profile(active_profile)
    _emit(
        events,
        'startup_check_started',
        mode=resolved_mode.mode,
        profile_present=normalized_profile is not None,
    )

    if resolved_mode.mode not in ALLOWED_RUNTIME_MODES:
        return _finalize(
            events,
            status='startup_blocked',
            mode='blocked',
            profile=normalized_profile,
            reasons=(f'invalid_deployment_mode:{resolved_mode.mode}',),
        )

    common_reasons = _collect_common_reasons(resolved_prereqs)
    if common_reasons:
        return _finalize(
            events,
            status='startup_blocked',
            mode=resolved_mode.mode,
            profile=normalized_profile,
            reasons=tuple(common_reasons),
        )

    if resolved_mode.mode == 'disabled':
        _emit(events, 'startup_disabled_confirmed', reason='deployment_disabled')
        return _finalize(
            events,
            status='startup_ready',
            mode='disabled',
            profile=None,
            reasons=('deployment_disabled',),
        )

    if normalized_profile is None:
        return _finalize(
            events,
            status='startup_blocked',
            mode=resolved_mode.mode,
            profile=None,
            reasons=('missing_required_profile',),
        )

    profile_reason = _validate_profile_for_mode(resolved_mode.mode, normalized_profile)
    if profile_reason is not None:
        return _finalize(
            events,
            status='startup_blocked',
            mode=resolved_mode.mode,
            profile=normalized_profile,
            reasons=(profile_reason,),
        )

    if resolved_mode.mode == 'dry_run':
        _emit(events, 'startup_route_confirmed', route='dry_run_session')
        return _finalize(
            events,
            status='startup_ready',
            mode='dry_run',
            profile=normalized_profile,
            reasons=('dry_run_path_ready',),
        )

    live_reasons = _collect_live_reasons(resolved_prereqs)
    if live_reasons:
        return _finalize(
            events,
            status='startup_dry_run_only',
            mode=resolved_mode.mode,
            profile=normalized_profile,
            reasons=tuple(live_reasons),
        )

    _emit(events, 'startup_route_confirmed', route='session_controller')
    return _finalize(
        events,
        status='startup_ready',
        mode=resolved_mode.mode,
        profile=normalized_profile,
        reasons=(f'{resolved_mode.mode}_path_ready',),
    )


def _collect_common_reasons(prereqs: StartupPrerequisites) -> list[str]:
    reasons: list[str] = []
    if not _file_exists(prereqs.runtime_launcher_script_path):
        reasons.append('missing_runtime_launcher_script')
    if not _file_exists(prereqs.session_report_script_path):
        reasons.append('missing_session_report_script')
    if not _file_exists(prereqs.reconcile_last_trade_script_path):
        reasons.append('missing_reconcile_last_trade_script')
    if not _artifact_path_valid(prereqs.session_event_log_path):
        reasons.append('invalid_session_event_log_path')
    if not _artifact_path_valid(prereqs.session_report_output_path):
        reasons.append('invalid_session_report_output_path')
    if not _artifact_path_valid(prereqs.reconciliation_payload_path):
        reasons.append('invalid_reconciliation_payload_path')
    return reasons


def _collect_live_reasons(prereqs: StartupPrerequisites) -> list[str]:
    reasons: list[str] = []
    if not _file_exists(prereqs.readiness_probe_script_path):
        reasons.append('missing_readiness_probe_script')
    if not _file_exists(prereqs.readiness_probe_config_path):
        reasons.append('missing_readiness_probe_config')
    if not _file_exists(prereqs.first_live_trade_script_path):
        reasons.append('missing_first_live_trade_script')
    return reasons


def _validate_profile_for_mode(mode: str, profile: dict[str, Any]) -> str | None:
    has_approval = all(key in profile for key in ('approval_id', 'approved_by', 'approved_at'))
    if mode == 'promoted_live' and not has_approval:
        return 'invalid_promoted_profile'
    if mode in {'capped_live', 'rolled_back'} and has_approval:
        return 'invalid_non_capped_profile'
    return None


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


def _file_exists(raw_path: str) -> bool:
    path = Path(raw_path)
    return path.exists() and path.is_file()


def _artifact_path_valid(raw_path: str) -> bool:
    path = Path(raw_path)
    parent = path.parent
    return parent.exists() and parent.is_dir()


def _finalize(
    events: list[StartupCheckEvent],
    *,
    status: str,
    mode: str,
    profile: dict[str, Any] | None,
    reasons: tuple[str, ...],
) -> StartupCheckResult:
    _emit(
        events,
        'startup_verdict_emitted',
        status=status,
        mode=mode,
        reasons=list(reasons),
        profile=profile,
    )
    return StartupCheckResult(
        status=status,
        mode=mode,
        profile=profile,
        reasons=reasons,
        events=tuple(events),
    )
