from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from execution.deployment_mode import VALID_DEPLOYMENT_MODES
from execution.promotion_gate import session_caps_from_dict
from scripts.run_live_session import DEFAULTS_PATH, run_live_session


ALLOWED_RUNTIME_MODES = VALID_DEPLOYMENT_MODES - {'blocked'}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['RuntimeLaunchEvent'], event_type: str, **details: Any) -> None:
    events.append(
        RuntimeLaunchEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


@dataclass(frozen=True)
class DeploymentModeState:
    mode: str
    profile: dict[str, Any] | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperatorStartRequest:
    session_id: str
    rounds: tuple[dict[str, Any], ...]
    expected_mode: str | None = None


@dataclass(frozen=True)
class RuntimeLaunchEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeLaunchResult:
    status: str
    route: str
    profile: dict[str, Any] | None
    reasons: tuple[str, ...]
    session_result: dict[str, Any] | None
    events: tuple[RuntimeLaunchEvent, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


class SessionRunner(Protocol):
    def __call__(self, session_config: dict[str, Any]) -> dict[str, Any]: ...


def deployment_mode_state_from_dict(payload: Mapping[str, Any]) -> DeploymentModeState:
    return DeploymentModeState(
        mode=str(payload['mode']),
        profile=None if payload.get('profile') is None else dict(payload['profile']),
        reasons=tuple(str(reason) for reason in payload.get('reasons', [])),
    )


def operator_start_request_from_dict(payload: Mapping[str, Any]) -> OperatorStartRequest:
    rounds = payload.get('rounds')
    if not isinstance(rounds, list):
        raise ValueError('invalid_start_rounds')
    return OperatorStartRequest(
        session_id=str(payload['session_id']),
        rounds=tuple(dict(round_payload) for round_payload in rounds if isinstance(round_payload, dict)),
        expected_mode=None if payload.get('expected_mode') is None else str(payload['expected_mode']),
    )


def launch_active_mode(
    *,
    deployment_mode: DeploymentModeState | Mapping[str, Any],
    active_profile: Mapping[str, Any] | None,
    operator_start_request: OperatorStartRequest | Mapping[str, Any],
    session_runner: SessionRunner | None = None,
    env: Mapping[str, str] | None = None,
    session_verifier: Any = None,
    adapter: Any = None,
    session_event_log_path: Path | None = None,
    defaults_path: Path = DEFAULTS_PATH,
) -> RuntimeLaunchResult:
    resolved_mode = (
        deployment_mode
        if isinstance(deployment_mode, DeploymentModeState)
        else deployment_mode_state_from_dict(deployment_mode)
    )
    try:
        resolved_request = (
            operator_start_request
            if isinstance(operator_start_request, OperatorStartRequest)
            else operator_start_request_from_dict(operator_start_request)
        )
    except (KeyError, TypeError, ValueError):
        return RuntimeLaunchResult(
            status='blocked',
            route='none',
            profile=None,
            reasons=('invalid_start_request',),
            session_result=None,
            events=(
                RuntimeLaunchEvent(
                    event_type='runtime_launch_verdict_emitted',
                    timestamp=_utc_now(),
                    details={'status': 'blocked', 'reasons': ['invalid_start_request'], 'route': 'none'},
                ),
            ),
        )

    events: list[RuntimeLaunchEvent] = []
    _emit(
        events,
        'runtime_launch_requested',
        mode=resolved_mode.mode,
        expected_mode=resolved_request.expected_mode,
        session_id=resolved_request.session_id,
        rounds=len(resolved_request.rounds),
    )

    if resolved_mode.mode not in ALLOWED_RUNTIME_MODES:
        return _finalize(
            events,
            status='blocked',
            route='none',
            profile=None,
            reasons=(f'invalid_runtime_mode:{resolved_mode.mode}',),
            session_result=None,
        )
    if resolved_request.expected_mode is not None and resolved_request.expected_mode != resolved_mode.mode:
        return _finalize(
            events,
            status='blocked',
            route='none',
            profile=_normalize_profile(active_profile),
            reasons=('conflicting_mode_request',),
            session_result=None,
        )
    if resolved_mode.mode == 'disabled':
        _emit(events, 'runtime_launch_disabled', reason='deployment_disabled')
        return _finalize(
            events,
            status='disabled',
            route='disabled',
            profile=None,
            reasons=('deployment_disabled',),
            session_result=None,
        )

    normalized_profile = _normalize_profile(active_profile)
    if normalized_profile is None:
        return _finalize(
            events,
            status='blocked',
            route='none',
            profile=None,
            reasons=('missing_required_profile',),
            session_result=None,
        )

    mode_profile = _resolve_mode_profile(resolved_mode, normalized_profile)
    if mode_profile is None:
        return _finalize(
            events,
            status='blocked',
            route='none',
            profile=normalized_profile,
            reasons=('missing_required_profile',),
            session_result=None,
        )
    if mode_profile != normalized_profile:
        return _finalize(
            events,
            status='blocked',
            route='none',
            profile=normalized_profile,
            reasons=('conflicting_profile_state',),
            session_result=None,
        )
    if not resolved_request.rounds:
        return _finalize(
            events,
            status='blocked',
            route='none',
            profile=normalized_profile,
            reasons=('invalid_start_rounds',),
            session_result=None,
        )

    session_payload = _build_session_payload(
        mode=resolved_mode.mode,
        profile=normalized_profile,
        request=resolved_request,
    )
    route = 'dry_run_session' if resolved_mode.mode == 'dry_run' else 'session_controller'
    _emit(
        events,
        'runtime_launch_routed',
        mode=resolved_mode.mode,
        route=route,
        requested_mode=session_payload['requested_mode'],
        live_trading_enabled=session_payload['live_trading_enabled'],
        session_caps={
            'max_trades_per_session': session_payload['max_trades_per_session'],
            'max_notional_per_session': session_payload['max_notional_per_session'],
            'max_consecutive_losses': session_payload['max_consecutive_losses'],
            'max_feed_age_seconds': session_payload['max_feed_age_seconds'],
        },
    )

    runner = session_runner or _build_default_session_runner(
        env=env,
        session_verifier=session_verifier,
        adapter=adapter,
        session_event_log_path=session_event_log_path,
        defaults_path=defaults_path,
    )
    try:
        session_result = runner(session_payload)
    except Exception as exc:
        return _finalize(
            events,
            status='blocked',
            route=route,
            profile=normalized_profile,
            reasons=(f'runtime_launch_failed:{exc}',),
            session_result=None,
        )
    return _finalize(
        events,
        status=resolved_mode.mode,
        route=route,
        profile=normalized_profile,
        reasons=(f'{resolved_mode.mode}_launched',),
        session_result=session_result,
    )


def _build_default_session_runner(
    *,
    env: Mapping[str, str] | None,
    session_verifier: Any,
    adapter: Any,
    session_event_log_path: Path | None,
    defaults_path: Path,
) -> SessionRunner:
    def _runner(session_config: dict[str, Any]) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json', delete=True) as handle:
            json.dump(session_config, handle)
            handle.flush()
            result = run_live_session(
                Path(handle.name),
                env=env,
                session_verifier=session_verifier,
                adapter=adapter,
                event_log_path=session_event_log_path,
                defaults_path=defaults_path,
            )
        if hasattr(result, 'events'):
            event_count = len(result.events)
        else:
            event_count = 0
        return {
            'status': getattr(result, 'status', 'unknown'),
            'stop_reason': getattr(result, 'stop_reason', None),
            'trades_attempted': getattr(result, 'trades_attempted', 0),
            'total_notional': getattr(result, 'total_notional', 0.0),
            'consecutive_losses': getattr(result, 'consecutive_losses', 0),
            'event_count': event_count,
        }

    return _runner


def _resolve_mode_profile(mode_state: DeploymentModeState, active_profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if mode_state.mode == 'dry_run':
        if mode_state.profile is None:
            return active_profile
        return _normalize_profile(mode_state.profile)
    if mode_state.mode in {'capped_live', 'promoted_live', 'rolled_back'}:
        return _normalize_profile(mode_state.profile) if mode_state.profile is not None else None
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


def _build_session_payload(
    *,
    mode: str,
    profile: dict[str, Any],
    request: OperatorStartRequest,
) -> dict[str, Any]:
    requested_mode = 'dry_run' if mode == 'dry_run' else 'live'
    live_trading_enabled = mode != 'dry_run'
    return {
        'session_id': request.session_id,
        'requested_mode': requested_mode,
        'live_trading_enabled': live_trading_enabled,
        'max_trades_per_session': profile['max_trades_per_session'],
        'max_notional_per_session': profile['max_notional_per_session'],
        'max_consecutive_losses': profile['max_consecutive_losses'],
        'max_feed_age_seconds': profile['max_feed_age_seconds'],
        'rounds': [dict(round_payload) for round_payload in request.rounds],
    }


def _finalize(
    events: list[RuntimeLaunchEvent],
    *,
    status: str,
    route: str,
    profile: dict[str, Any] | None,
    reasons: tuple[str, ...],
    session_result: dict[str, Any] | None,
) -> RuntimeLaunchResult:
    _emit(
        events,
        'runtime_launch_verdict_emitted',
        status=status,
        route=route,
        reasons=list(reasons),
        profile=profile,
        session_result=session_result,
    )
    return RuntimeLaunchResult(
        status=status,
        route=route,
        profile=profile,
        reasons=reasons,
        session_result=session_result,
        events=tuple(events),
    )
