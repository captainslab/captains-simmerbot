from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from execution.promotion_gate import SessionCaps, session_caps_from_dict
from reporting.performance_summary import PerformanceSummary, performance_summary_from_dict


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['RollbackEvent'], event_type: str, **details: Any) -> None:
    events.append(
        RollbackEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


@dataclass(frozen=True)
class PromotionState:
    status: str
    profile: dict[str, Any]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RollbackTriggerConfig:
    operator_requested: bool = False
    mismatch_threshold: int = 2
    blocked_session_threshold: int = 2

    def __post_init__(self) -> None:
        if self.mismatch_threshold <= 0:
            raise ValueError('invalid_mismatch_threshold')
        if self.blocked_session_threshold <= 0:
            raise ValueError('invalid_blocked_session_threshold')


@dataclass(frozen=True)
class RollbackEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RollbackDecision:
    status: str
    profile: dict[str, Any]
    reasons: tuple[str, ...]
    events: tuple[RollbackEvent, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


def promotion_state_from_dict(payload: Mapping[str, Any]) -> PromotionState:
    return PromotionState(
        status=str(payload['status']),
        profile=dict(payload.get('profile') or {}),
        reasons=tuple(str(reason) for reason in payload.get('reasons', [])),
    )


def rollback_trigger_config_from_dict(payload: Mapping[str, Any]) -> RollbackTriggerConfig:
    return RollbackTriggerConfig(
        operator_requested=bool(payload.get('operator_requested', False)),
        mismatch_threshold=int(payload.get('mismatch_threshold', 2)),
        blocked_session_threshold=int(payload.get('blocked_session_threshold', 2)),
    )


def evaluate_rollback(
    *,
    promotion_state: PromotionState | Mapping[str, Any],
    current_profile: Mapping[str, Any],
    prior_safe_profile: Mapping[str, Any] | None,
    summary: PerformanceSummary | Mapping[str, Any],
    trigger_config: RollbackTriggerConfig | Mapping[str, Any],
) -> RollbackDecision:
    resolved_state = (
        promotion_state
        if isinstance(promotion_state, PromotionState)
        else promotion_state_from_dict(promotion_state)
    )
    resolved_summary = summary if isinstance(summary, PerformanceSummary) else performance_summary_from_dict(summary)
    resolved_trigger = (
        trigger_config
        if isinstance(trigger_config, RollbackTriggerConfig)
        else rollback_trigger_config_from_dict(trigger_config)
    )
    events: list[RollbackEvent] = []

    current_result = _normalize_profile(current_profile, reason='invalid_active_profile')
    if current_result is None:
        return _finalize(
            events,
            status='blocked',
            profile={},
            reasons=('invalid_active_profile',),
        )
    resolved_current_profile, current_caps = current_result
    _emit(
        events,
        'rollback_evaluation_started',
        promotion_status=resolved_state.status,
        current_profile=resolved_current_profile,
        promotion_reasons=list(resolved_state.reasons),
    )

    if resolved_state.status not in {'promoted', 'remain_capped', 'blocked'}:
        return _finalize(
            events,
            status='blocked',
            profile=resolved_current_profile,
            reasons=('invalid_active_profile',),
        )

    if resolved_state.status != 'promoted':
        return _finalize(
            events,
            status='keep_current',
            profile=resolved_current_profile,
            reasons=('active_profile_not_promoted',),
        )

    prior_result = _normalize_profile(prior_safe_profile, reason='invalid_prior_safe_profile')
    if prior_result is None:
        return _finalize(
            events,
            status='blocked',
            profile=resolved_current_profile,
            reasons=('invalid_prior_safe_profile',),
        )
    resolved_prior_profile, prior_caps = prior_result

    trigger_reasons: list[str] = []
    if resolved_trigger.operator_requested:
        trigger_reasons.append('operator_rollback_requested')
    if resolved_summary.reconciliation_unresolved_count > 0:
        trigger_reasons.append('unresolved_reconciliation_present')
    if resolved_summary.reconciliation_mismatch_count >= resolved_trigger.mismatch_threshold:
        trigger_reasons.append('repeated_reconciliation_mismatch')
    if resolved_summary.blocked_session_count >= resolved_trigger.blocked_session_threshold:
        trigger_reasons.append('repeated_blocked_sessions')
    if resolved_summary.promotion_verdict == 'blocked':
        trigger_reasons.append('promotion_summary_blocked')

    if not trigger_reasons:
        return _finalize(
            events,
            status='keep_current',
            profile=resolved_current_profile,
            reasons=('rollback_not_triggered',),
        )

    if not _is_capped_safe(prior_caps, current_caps):
        return _finalize(
            events,
            status='blocked',
            profile=resolved_current_profile,
            reasons=('invalid_prior_safe_profile',),
        )

    _emit(
        events,
        'rollback_profile_applied',
        restored_profile=resolved_prior_profile,
        trigger_reasons=trigger_reasons,
    )
    return _finalize(
        events,
        status='rolled_back',
        profile=resolved_prior_profile,
        reasons=tuple(trigger_reasons),
    )


def _normalize_profile(
    payload: Mapping[str, Any] | None,
    *,
    reason: str,
) -> tuple[dict[str, Any], SessionCaps] | None:
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
    return normalized, caps


def _is_capped_safe(prior_caps: SessionCaps, current_caps: SessionCaps) -> bool:
    return (
        prior_caps.max_trades_per_session <= current_caps.max_trades_per_session
        and prior_caps.max_notional_per_session <= current_caps.max_notional_per_session
        and prior_caps.max_consecutive_losses <= current_caps.max_consecutive_losses
        and prior_caps.max_feed_age_seconds <= current_caps.max_feed_age_seconds
    )


def _finalize(
    events: list[RollbackEvent],
    *,
    status: str,
    profile: dict[str, Any],
    reasons: tuple[str, ...],
) -> RollbackDecision:
    _emit(
        events,
        'rollback_verdict_emitted',
        rollback_status=status,
        reasons=list(reasons),
        profile=profile,
    )
    return RollbackDecision(
        status=status,
        profile=profile,
        reasons=reasons,
        events=tuple(events),
    )
