from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _coerce_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    normalized = value.replace('Z', '+00:00')
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


@dataclass(frozen=True)
class FeatureSignal:
    name: str
    family: str
    direction: str
    raw_value: float
    strength: float
    weight: float
    reason: str


@dataclass(frozen=True)
class FeatureSnapshot:
    market_id: str
    observed_at: str
    age_seconds: float
    available_balance_usdc: float | None
    health_state: str
    signals: tuple[FeatureSignal, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeatureSnapshotInput:
    market_id: str
    observed_at: str | datetime
    market_price: float
    reference_price: float
    momentum: float
    yes_pressure: float
    no_pressure: float
    available_balance_usdc: float | None
    health_state: str = 'ok'


@dataclass(frozen=True)
class FeatureSnapshotBuilderConfig:
    momentum_neutral_band: float = 0.0005
    momentum_scale: float = 0.004
    price_delta_neutral_band: float = 0.001
    price_delta_scale: float = 0.004
    market_pressure_neutral_band: float = 0.05
    market_pressure_scale: float = 0.40
    momentum_weight: float = 0.55
    price_delta_weight: float = 0.55
    market_pressure_weight: float = 0.45


def _build_signal(
    *,
    name: str,
    family: str,
    raw_value: float,
    neutral_band: float,
    scale: float,
    weight: float,
) -> FeatureSignal:
    if raw_value > neutral_band:
        direction = 'yes'
    elif raw_value < -neutral_band:
        direction = 'no'
    else:
        direction = 'neutral'

    if direction == 'neutral':
        strength = 0.0
    else:
        strength = _clamp(abs(raw_value) / max(scale, 1e-9), 0.0, 1.0)

    return FeatureSignal(
        name=name,
        family=family,
        direction=direction,
        raw_value=raw_value,
        strength=strength,
        weight=weight,
        reason=f'{name}={raw_value:+.6f}',
    )


def build_feature_snapshot(
    inputs: FeatureSnapshotInput,
    *,
    config: FeatureSnapshotBuilderConfig | None = None,
    now: datetime | None = None,
) -> FeatureSnapshot:
    resolved = config or FeatureSnapshotBuilderConfig()
    observed_at = _coerce_datetime(inputs.observed_at)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    age_seconds = max(0.0, (current_time - observed_at).total_seconds())
    reference_price = float(inputs.reference_price)
    if reference_price <= 0:
        price_delta = 0.0
    else:
        price_delta = (reference_price - float(inputs.market_price)) / reference_price

    total_pressure = max(0.0, float(inputs.yes_pressure)) + max(0.0, float(inputs.no_pressure))
    if total_pressure <= 0:
        market_pressure = 0.0
    else:
        market_pressure = (float(inputs.yes_pressure) - float(inputs.no_pressure)) / total_pressure

    signals = (
        _build_signal(
            name='momentum',
            family='price_direction',
            raw_value=float(inputs.momentum),
            neutral_band=resolved.momentum_neutral_band,
            scale=resolved.momentum_scale,
            weight=resolved.momentum_weight,
        ),
        _build_signal(
            name='price_delta',
            family='price_direction',
            raw_value=price_delta,
            neutral_band=resolved.price_delta_neutral_band,
            scale=resolved.price_delta_scale,
            weight=resolved.price_delta_weight,
        ),
        _build_signal(
            name='market_pressure',
            family='market_pressure',
            raw_value=market_pressure,
            neutral_band=resolved.market_pressure_neutral_band,
            scale=resolved.market_pressure_scale,
            weight=resolved.market_pressure_weight,
        ),
    )

    return FeatureSnapshot(
        market_id=inputs.market_id,
        observed_at=observed_at.isoformat(),
        age_seconds=age_seconds,
        available_balance_usdc=inputs.available_balance_usdc,
        health_state=inputs.health_state,
        signals=signals,
        metadata={
            'market_price': float(inputs.market_price),
            'reference_price': reference_price,
            'momentum': float(inputs.momentum),
            'price_delta': price_delta,
            'market_pressure': market_pressure,
        },
    )
