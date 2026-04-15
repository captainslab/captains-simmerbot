from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from adapters.clob_auth_validator import AuthValidationResult
from engine.position_sizer import PositionSizingResult, SizingBalanceSnapshot

if TYPE_CHECKING:
    from execution.trade_executor import DecisionRecord


VALID_ACTIONS = {'buy_yes', 'buy_no', 'no_trade'}


def _append_unique(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace('Z', '+00:00')
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ReadinessGateConfig:
    min_order_size: float = 1.0
    max_balance_age_seconds: float = 30.0
    max_market_age_seconds: float = 30.0
    required_health_state: str = 'ok'


@dataclass(frozen=True)
class ReadinessGateResult:
    status: str
    reasons: tuple[str, ...]


def evaluate_readiness(
    *,
    decision: 'DecisionRecord',
    sizing: PositionSizingResult | None,
    balance_snapshot: SizingBalanceSnapshot | None,
    auth_result: AuthValidationResult,
    requested_mode: str,
    live_trading_enabled: bool,
    config: ReadinessGateConfig | None = None,
    now: datetime | None = None,
) -> ReadinessGateResult:
    resolved = config or ReadinessGateConfig()
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=timezone.utc)

    hard_reasons: list[str] = []
    feature_summary = decision.feature_summary or {}

    if requested_mode not in {'live', 'dry_run'}:
        _append_unique(hard_reasons, f'invalid_execution_mode:{requested_mode}')

    if decision.action not in VALID_ACTIONS:
        _append_unique(hard_reasons, f'invalid_action:{decision.action}')

    market_time = _parse_timestamp(feature_summary.get('observed_at'))
    if market_time is None:
        _append_unique(hard_reasons, 'missing_market_timestamp')
    else:
        market_age = max(0.0, (observed_now - market_time).total_seconds())
        if market_age > resolved.max_market_age_seconds:
            _append_unique(hard_reasons, f'stale_market:{market_age:.1f}s')

    health_state = str(feature_summary.get('health_state') or 'unknown')
    if health_state != resolved.required_health_state:
        _append_unique(hard_reasons, f'health_state:{health_state}')

    if decision.action == 'no_trade':
        if hard_reasons:
            return ReadinessGateResult(status='blocked', reasons=tuple(hard_reasons))
        return ReadinessGateResult(status='ready_dry_run', reasons=('no_trade_action',))

    if balance_snapshot is None or balance_snapshot.available_usdc is None:
        _append_unique(hard_reasons, 'missing_balance')
    balance_time = _parse_timestamp(balance_snapshot.fetched_at if balance_snapshot else None)
    if balance_time is None:
        _append_unique(hard_reasons, 'missing_balance_timestamp')
    else:
        balance_age = max(0.0, (observed_now - balance_time).total_seconds())
        if balance_age > resolved.max_balance_age_seconds:
            _append_unique(hard_reasons, f'stale_balance:{balance_age:.1f}s')

    if sizing is None:
        _append_unique(hard_reasons, 'missing_sizing_result')
    else:
        if not sizing.allowed:
            for reason in sizing.reasons:
                if reason != 'no_trade_action':
                    _append_unique(hard_reasons, reason)
        if sizing.size <= 0 or sizing.notional <= 0:
            _append_unique(hard_reasons, 'invalid_sized_order')
        elif sizing.size < resolved.min_order_size:
            _append_unique(hard_reasons, f'below_minimum_size:{sizing.size:.4f}<{resolved.min_order_size:.4f}')

    if hard_reasons:
        return ReadinessGateResult(status='blocked', reasons=tuple(hard_reasons))

    dry_run_reasons: list[str] = []
    if requested_mode == 'live' and not live_trading_enabled:
        _append_unique(dry_run_reasons, 'live_trading_disabled')

    if auth_result.status == 'auth_blocked':
        if requested_mode == 'live':
            return ReadinessGateResult(status='blocked', reasons=auth_result.reasons or ('auth_blocked',))
        return ReadinessGateResult(status='ready_dry_run', reasons=auth_result.reasons or ('auth_blocked',))
    if auth_result.status == 'auth_dry_run_only':
        for reason in auth_result.reasons or ('auth_dry_run_only',):
            _append_unique(dry_run_reasons, reason)

    if requested_mode == 'live':
        if dry_run_reasons:
            return ReadinessGateResult(status='ready_dry_run', reasons=tuple(dry_run_reasons))
        return ReadinessGateResult(status='ready_live', reasons=())

    return ReadinessGateResult(status='ready_dry_run', reasons=tuple(dry_run_reasons))
