from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from execution.promotion_gate import SessionCaps, session_caps_from_dict


VALID_DEPLOYMENT_MODES = {'disabled', 'dry_run', 'capped_live', 'promoted_live', 'rolled_back'}
ALLOWED_TRANSITIONS = {
    'disabled': {'disabled', 'dry_run'},
    'dry_run': {'disabled', 'dry_run', 'capped_live'},
    'capped_live': {'disabled', 'dry_run', 'capped_live', 'promoted_live'},
    'promoted_live': {'disabled', 'dry_run', 'promoted_live', 'rolled_back'},
    'rolled_back': {'disabled', 'dry_run', 'rolled_back', 'capped_live'},
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['DeploymentModeEvent'], event_type: str, **details: Any) -> None:
    events.append(
        DeploymentModeEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


@dataclass(frozen=True)
class ProfileState:
    profile: dict[str, Any]


@dataclass(frozen=True)
class ControlState:
    status: str
    profile: dict[str, Any]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperatorModeRequest:
    current_mode: str
    requested_mode: str


@dataclass(frozen=True)
class DeploymentModeEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeploymentModeDecision:
    mode: str
    profile: dict[str, Any] | None
    reasons: tuple[str, ...]
    events: tuple[DeploymentModeEvent, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


def profile_state_from_dict(payload: Mapping[str, Any]) -> ProfileState:
    return ProfileState(profile=dict(payload))


def control_state_from_dict(payload: Mapping[str, Any]) -> ControlState:
    return ControlState(
        status=str(payload['status']),
        profile=dict(payload.get('profile') or {}),
        reasons=tuple(str(reason) for reason in payload.get('reasons', [])),
    )


def operator_mode_request_from_dict(payload: Mapping[str, Any]) -> OperatorModeRequest:
    return OperatorModeRequest(
        current_mode=str(payload['current_mode']),
        requested_mode=str(payload['requested_mode']),
    )


def evaluate_deployment_mode(
    *,
    profile_state: ProfileState | Mapping[str, Any] | None,
    promotion_state: ControlState | Mapping[str, Any] | None,
    rollback_state: ControlState | Mapping[str, Any] | None,
    operator_request: OperatorModeRequest | Mapping[str, Any],
) -> DeploymentModeDecision:
    resolved_request = (
        operator_request
        if isinstance(operator_request, OperatorModeRequest)
        else operator_mode_request_from_dict(operator_request)
    )
    resolved_profile_state = None
    if profile_state is not None:
        resolved_profile_state = (
            profile_state if isinstance(profile_state, ProfileState) else profile_state_from_dict(profile_state)
        )
    resolved_promotion_state = None
    if promotion_state is not None:
        resolved_promotion_state = (
            promotion_state if isinstance(promotion_state, ControlState) else control_state_from_dict(promotion_state)
        )
    resolved_rollback_state = None
    if rollback_state is not None:
        resolved_rollback_state = (
            rollback_state if isinstance(rollback_state, ControlState) else control_state_from_dict(rollback_state)
        )

    events: list[DeploymentModeEvent] = []
    _emit(
        events,
        'deployment_mode_evaluated',
        current_mode=resolved_request.current_mode,
        requested_mode=resolved_request.requested_mode,
    )

    if resolved_request.current_mode not in VALID_DEPLOYMENT_MODES:
        return _finalize(events, mode='blocked', profile=None, reasons=(f'invalid_current_mode:{resolved_request.current_mode}',))
    if resolved_request.requested_mode not in VALID_DEPLOYMENT_MODES:
        return _finalize(events, mode='blocked', profile=None, reasons=(f'invalid_requested_mode:{resolved_request.requested_mode}',))
    if resolved_request.requested_mode not in ALLOWED_TRANSITIONS[resolved_request.current_mode]:
        return _finalize(
            events,
            mode='blocked',
            profile=_normalized_profile_or_none(resolved_profile_state),
            reasons=(f'invalid_mode_transition:{resolved_request.current_mode}->{resolved_request.requested_mode}',),
        )

    if resolved_request.requested_mode == 'disabled':
        return _finalize(events, mode='disabled', profile=None, reasons=('deployment_disabled',))
    if resolved_request.requested_mode == 'dry_run':
        return _finalize(events, mode='dry_run', profile=None, reasons=('dry_run_enabled',))
    if resolved_request.requested_mode == 'capped_live':
        current_profile = _require_capped_profile(resolved_profile_state)
        if current_profile is None:
            return _finalize(events, mode='blocked', profile=_normalized_profile_or_none(resolved_profile_state), reasons=('invalid_capped_safe_profile',))
        return _finalize(events, mode='capped_live', profile=current_profile, reasons=('capped_live_enabled',))
    if resolved_request.requested_mode == 'promoted_live':
        if resolved_promotion_state is None:
            return _finalize(events, mode='blocked', profile=_normalized_profile_or_none(resolved_profile_state), reasons=('missing_promotion_state',))
        promoted_profile = _require_promoted_profile(resolved_promotion_state)
        if promoted_profile is None:
            return _finalize(
                events,
                mode='blocked',
                profile=_normalized_profile_or_none(resolved_profile_state),
                reasons=('invalid_promoted_profile_state',),
            )
        if resolved_rollback_state is not None and resolved_rollback_state.status == 'rolled_back':
            return _finalize(
                events,
                mode='blocked',
                profile=_normalized_profile_or_none(resolved_profile_state),
                reasons=('conflicting_rollback_state',),
            )
        return _finalize(events, mode='promoted_live', profile=promoted_profile, reasons=('promoted_live_enabled',))

    if resolved_rollback_state is None:
        return _finalize(events, mode='blocked', profile=_normalized_profile_or_none(resolved_profile_state), reasons=('missing_rollback_state',))
    rolled_back_profile = _require_rolled_back_profile(resolved_rollback_state)
    if rolled_back_profile is None:
        return _finalize(events, mode='blocked', profile=_normalized_profile_or_none(resolved_profile_state), reasons=('invalid_rollback_state',))
    return _finalize(events, mode='rolled_back', profile=rolled_back_profile, reasons=('rolled_back_enabled',))


def _normalized_profile_or_none(profile_state: ProfileState | None) -> dict[str, Any] | None:
    if profile_state is None:
        return None
    return _normalize_profile(profile_state.profile)


def _require_capped_profile(profile_state: ProfileState | None) -> dict[str, Any] | None:
    if profile_state is None:
        return None
    normalized = _normalize_profile(profile_state.profile)
    if normalized is None:
        return None
    if any(key in normalized for key in ('approval_id', 'approved_by', 'approved_at')):
        return None
    return normalized


def _require_promoted_profile(control_state: ControlState) -> dict[str, Any] | None:
    if control_state.status != 'promoted':
        return None
    normalized = _normalize_profile(control_state.profile)
    if normalized is None:
        return None
    if not all(key in normalized for key in ('approval_id', 'approved_by', 'approved_at')):
        return None
    return normalized


def _require_rolled_back_profile(control_state: ControlState) -> dict[str, Any] | None:
    if control_state.status != 'rolled_back':
        return None
    normalized = _normalize_profile(control_state.profile)
    if normalized is None:
        return None
    if any(key in normalized for key in ('approval_id', 'approved_by', 'approved_at')):
        return None
    return normalized


def _normalize_profile(payload: Mapping[str, Any]) -> dict[str, Any] | None:
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


def _finalize(
    events: list[DeploymentModeEvent],
    *,
    mode: str,
    profile: dict[str, Any] | None,
    reasons: tuple[str, ...],
) -> DeploymentModeDecision:
    _emit(
        events,
        'deployment_mode_verdict_emitted',
        mode=mode,
        reasons=list(reasons),
        profile=profile,
    )
    return DeploymentModeDecision(
        mode=mode,
        profile=profile,
        reasons=reasons,
        events=tuple(events),
    )
