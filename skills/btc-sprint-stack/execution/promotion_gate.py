from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from reporting.performance_summary import PerformanceSummary, performance_summary_from_dict


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['PromotionEvent'], event_type: str, **details: Any) -> None:
    events.append(
        PromotionEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


@dataclass(frozen=True)
class SessionCaps:
    max_trades_per_session: int
    max_notional_per_session: float
    max_consecutive_losses: int
    max_feed_age_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_trades_per_session <= 0:
            raise ValueError('invalid_current_max_trades_per_session')
        if self.max_notional_per_session <= 0:
            raise ValueError('invalid_current_max_notional_per_session')
        if self.max_consecutive_losses <= 0:
            raise ValueError('invalid_current_max_consecutive_losses')
        if self.max_feed_age_seconds <= 0:
            raise ValueError('invalid_current_max_feed_age_seconds')

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionApproval:
    approval_id: str
    approved: bool
    approved_by: str
    approved_at: str
    profile_name: str
    max_trades_per_session: int
    max_notional_per_session: float


@dataclass(frozen=True)
class PromotionEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromotionDecision:
    status: str
    profile: dict[str, Any]
    reasons: tuple[str, ...]
    events: tuple[PromotionEvent, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


def session_caps_from_dict(payload: Mapping[str, Any]) -> SessionCaps:
    return SessionCaps(
        max_trades_per_session=int(payload['max_trades_per_session']),
        max_notional_per_session=float(payload['max_notional_per_session']),
        max_consecutive_losses=int(payload['max_consecutive_losses']),
        max_feed_age_seconds=float(payload.get('max_feed_age_seconds', 30.0)),
    )


def promotion_approval_from_dict(payload: Mapping[str, Any]) -> PromotionApproval:
    return PromotionApproval(
        approval_id=str(payload['approval_id']),
        approved=bool(payload['approved']),
        approved_by=str(payload['approved_by']),
        approved_at=str(payload['approved_at']),
        profile_name=str(payload['profile_name']),
        max_trades_per_session=int(payload['max_trades_per_session']),
        max_notional_per_session=float(payload['max_notional_per_session']),
    )


def evaluate_promotion_review(
    *,
    summary: PerformanceSummary | Mapping[str, Any],
    current_caps: SessionCaps | Mapping[str, Any],
    approval: PromotionApproval | Mapping[str, Any] | None,
) -> PromotionDecision:
    resolved_summary = summary if isinstance(summary, PerformanceSummary) else performance_summary_from_dict(summary)
    resolved_caps = current_caps if isinstance(current_caps, SessionCaps) else session_caps_from_dict(current_caps)
    events: list[PromotionEvent] = []
    _emit(
        events,
        'promotion_review_started',
        promotion_verdict=resolved_summary.promotion_verdict,
        current_caps=resolved_caps.as_dict(),
    )

    if resolved_summary.reconciliation_unresolved_count > 0:
        return _finalize(
            events,
            status='blocked',
            profile=resolved_caps.as_dict(),
            reasons=('unresolved_reconciliation_present',),
        )
    if resolved_summary.promotion_verdict == 'blocked':
        reasons = resolved_summary.promotion_reason_codes or ('promotion_verdict_blocked',)
        return _finalize(
            events,
            status='blocked',
            profile=resolved_caps.as_dict(),
            reasons=tuple(reasons),
        )
    if resolved_summary.promotion_verdict != 'eligible_for_review':
        return _finalize(
            events,
            status='remain_capped',
            profile=resolved_caps.as_dict(),
            reasons=('promotion_verdict_not_eligible',) + tuple(resolved_summary.promotion_reason_codes),
        )
    if approval is None:
        return _finalize(
            events,
            status='blocked',
            profile=resolved_caps.as_dict(),
            reasons=('missing_promotion_approval',),
        )

    try:
        resolved_approval = approval if isinstance(approval, PromotionApproval) else promotion_approval_from_dict(approval)
    except (KeyError, TypeError, ValueError):
        return _finalize(
            events,
            status='blocked',
            profile=resolved_caps.as_dict(),
            reasons=('invalid_promotion_profile',),
        )

    invalid_reasons = _validate_approval(resolved_approval, resolved_caps)
    if invalid_reasons:
        return _finalize(
            events,
            status='blocked',
            profile=resolved_caps.as_dict(),
            reasons=tuple(invalid_reasons),
        )

    promoted_profile = {
        'profile_name': resolved_approval.profile_name,
        'approval_id': resolved_approval.approval_id,
        'approved_by': resolved_approval.approved_by,
        'approved_at': resolved_approval.approved_at,
        'max_trades_per_session': resolved_approval.max_trades_per_session,
        'max_notional_per_session': resolved_approval.max_notional_per_session,
        'max_consecutive_losses': resolved_caps.max_consecutive_losses,
        'max_feed_age_seconds': resolved_caps.max_feed_age_seconds,
    }
    _emit(
        events,
        'promotion_profile_applied',
        profile_name=resolved_approval.profile_name,
        approval_id=resolved_approval.approval_id,
        promoted_caps=promoted_profile,
    )
    return _finalize(
        events,
        status='promoted',
        profile=promoted_profile,
        reasons=('promotion_approved',),
    )


def _validate_approval(approval: PromotionApproval, caps: SessionCaps) -> list[str]:
    reasons: list[str] = []
    if not approval.approved:
        reasons.append('missing_promotion_approval')
    if not approval.approval_id.strip():
        reasons.append('invalid_promotion_profile')
    if not approval.approved_by.strip():
        reasons.append('invalid_promotion_profile')
    if not approval.profile_name.strip():
        reasons.append('invalid_promotion_profile')
    if approval.max_trades_per_session <= caps.max_trades_per_session:
        reasons.append('invalid_promotion_profile')
    if approval.max_notional_per_session <= caps.max_notional_per_session:
        reasons.append('invalid_promotion_profile')
    return reasons


def _finalize(
    events: list[PromotionEvent],
    *,
    status: str,
    profile: dict[str, Any],
    reasons: tuple[str, ...],
) -> PromotionDecision:
    _emit(
        events,
        'promotion_verdict_emitted',
        promotion_status=status,
        reasons=list(reasons),
        profile=profile,
    )
    return PromotionDecision(
        status=status,
        profile=profile,
        reasons=reasons,
        events=tuple(events),
    )
