from __future__ import annotations

from dataclasses import dataclass

from engine.feature_snapshot_builder import FeatureSnapshot


@dataclass(frozen=True)
class RiskGateConfig:
    min_edge: float = 0.70
    max_snapshot_age_seconds: float = 30.0
    min_balance_usdc: float = 1.0
    required_health_state: str = 'ok'


@dataclass(frozen=True)
class RiskGateResult:
    allowed: bool
    reasons: tuple[str, ...]
    observed_edge: float


def evaluate_risk_gate(
    snapshot: FeatureSnapshot,
    *,
    edge: float,
    config: RiskGateConfig | None = None,
) -> RiskGateResult:
    resolved = config or RiskGateConfig()
    reasons: list[str] = []

    if snapshot.age_seconds > resolved.max_snapshot_age_seconds:
        reasons.append(f'stale_data:{snapshot.age_seconds:.1f}s')
    if edge < resolved.min_edge:
        reasons.append(f'weak_edge:{edge:.4f}')
    if snapshot.available_balance_usdc is None:
        reasons.append('missing_balance')
    elif snapshot.available_balance_usdc < resolved.min_balance_usdc:
        reasons.append(f'balance_below_minimum:{snapshot.available_balance_usdc:.2f}')
    if snapshot.health_state != resolved.required_health_state:
        reasons.append(f'health_state:{snapshot.health_state}')

    return RiskGateResult(
        allowed=not reasons,
        reasons=tuple(reasons),
        observed_edge=edge,
    )
