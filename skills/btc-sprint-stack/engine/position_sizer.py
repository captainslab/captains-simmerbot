from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from execution.trade_executor import DecisionRecord


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class SizingBalanceSnapshot:
    available_usdc: float | None
    fetched_at: str | None


@dataclass(frozen=True)
class PositionSizerConfig:
    min_size: float = 1.0
    max_size: float = 4.0
    max_balance_fraction: float = 0.25
    max_balance_age_seconds: float = 30.0
    min_edge: float = 0.70
    edge_for_max_size: float = 1.0


@dataclass(frozen=True)
class PositionSizingResult:
    allowed: bool
    size: float
    notional: float
    proposed_size: float
    scale_factor: float
    reasons: tuple[str, ...]


class PositionSizer:
    def __init__(self, *, config: PositionSizerConfig | None = None) -> None:
        self._config = config or PositionSizerConfig()

    def size(
        self,
        *,
        decision: 'DecisionRecord',
        balance_snapshot: SizingBalanceSnapshot | None,
        now: datetime | None = None,
    ) -> PositionSizingResult:
        if decision.action == 'no_trade':
            return PositionSizingResult(
                allowed=False,
                size=0.0,
                notional=0.0,
                proposed_size=0.0,
                scale_factor=0.0,
                reasons=('no_trade_action',),
            )

        reasons: list[str] = []
        gate_allowed = decision.gate_result.get('allowed')
        if gate_allowed is False:
            gate_reasons = decision.gate_result.get('reasons') or []
            reasons.extend(str(reason) for reason in gate_reasons)
            if not gate_reasons:
                reasons.append('gate_blocked')

        health_state = str(decision.feature_summary.get('health_state') or 'unknown')
        if health_state != 'ok':
            reasons.append(f'health_state:{health_state}')
        if decision.edge < self._config.min_edge:
            reasons.append(f'weak_edge:{decision.edge:.4f}')

        if balance_snapshot is None or balance_snapshot.available_usdc is None:
            reasons.append('missing_balance')
            available_usdc = 0.0
        else:
            available_usdc = float(balance_snapshot.available_usdc)
            if available_usdc <= 0:
                reasons.append(f'balance_below_minimum:{available_usdc:.2f}')

        balance_time = _parse_timestamp(balance_snapshot.fetched_at if balance_snapshot else None)
        if balance_time is None:
            reasons.append('missing_balance_timestamp')
        else:
            observed_now = now or datetime.now(timezone.utc)
            if observed_now.tzinfo is None:
                observed_now = observed_now.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (observed_now - balance_time).total_seconds())
            if age_seconds > self._config.max_balance_age_seconds:
                reasons.append(f'stale_balance:{age_seconds:.1f}s')

        capped_size = min(
            max(float(decision.amount), 0.0),
            self._config.max_size,
            available_usdc,
            max(available_usdc * self._config.max_balance_fraction, 0.0),
        )
        scale_factor = min(1.0, max(decision.edge, 0.0) / max(self._config.edge_for_max_size, 1e-9))
        proposed_size = round(max(capped_size * scale_factor, 0.0), 4)

        if proposed_size < self._config.min_size and decision.action in {'buy_yes', 'buy_no'}:
            reasons.append(f'below_minimum_size:{proposed_size:.4f}<{self._config.min_size:.4f}')

        if reasons:
            return PositionSizingResult(
                allowed=False,
                size=0.0,
                notional=0.0,
                proposed_size=proposed_size,
                scale_factor=round(scale_factor, 4),
                reasons=tuple(reasons),
            )

        return PositionSizingResult(
            allowed=True,
            size=proposed_size,
            notional=proposed_size,
            proposed_size=proposed_size,
            scale_factor=round(scale_factor, 4),
            reasons=(),
        )
