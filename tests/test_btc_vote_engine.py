from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from engine.feature_snapshot_builder import FeatureSnapshotInput, build_feature_snapshot
from engine.risk_gate import RiskGateConfig
from engine.vote_engine import VoteEngine, VoteEngineConfig


def _build_engine(**risk_overrides) -> VoteEngine:
    return VoteEngine(
        config=VoteEngineConfig(
            risk_gate=RiskGateConfig(
                min_edge=risk_overrides.get('min_edge', 0.70),
                max_snapshot_age_seconds=risk_overrides.get('max_snapshot_age_seconds', 30.0),
                min_balance_usdc=risk_overrides.get('min_balance_usdc', 1.0),
                required_health_state=risk_overrides.get('required_health_state', 'ok'),
            )
        )
    )


def _build_snapshot(
    *,
    momentum: float,
    market_price: float,
    reference_price: float,
    yes_pressure: float,
    no_pressure: float,
    balance: float | None = 10.0,
    health_state: str = 'ok',
    age_seconds: float = 5.0,
):
    now = datetime.now(timezone.utc)
    return build_feature_snapshot(
        FeatureSnapshotInput(
            market_id='btc-5m',
            observed_at=now - timedelta(seconds=age_seconds),
            market_price=market_price,
            reference_price=reference_price,
            momentum=momentum,
            yes_pressure=yes_pressure,
            no_pressure=no_pressure,
            available_balance_usdc=balance,
            health_state=health_state,
        ),
        now=now,
    )


def test_buy_yes_path():
    engine = _build_engine(min_edge=0.70)
    snapshot = _build_snapshot(
        momentum=0.0040,
        market_price=99.6,
        reference_price=100.0,
        yes_pressure=80.0,
        no_pressure=20.0,
    )

    outcome = engine.decide(snapshot)

    assert outcome.action == 'buy_yes'
    assert outcome.edge >= 1.0
    assert outcome.blocked_reasons == ()


def test_buy_no_path():
    engine = _build_engine(min_edge=0.70)
    snapshot = _build_snapshot(
        momentum=-0.0040,
        market_price=100.4,
        reference_price=100.0,
        yes_pressure=20.0,
        no_pressure=80.0,
    )

    outcome = engine.decide(snapshot)

    assert outcome.action == 'buy_no'
    assert outcome.edge >= 1.0
    assert outcome.blocked_reasons == ()


def test_no_trade_on_weak_edge():
    engine = _build_engine(min_edge=0.30)
    snapshot = _build_snapshot(
        momentum=0.0008,
        market_price=99.92,
        reference_price=100.0,
        yes_pressure=50.0,
        no_pressure=50.0,
    )

    outcome = engine.decide(snapshot)

    assert outcome.action == 'no_trade'
    assert any(reason.startswith('weak_edge:') for reason in outcome.blocked_reasons)


def test_no_trade_on_stale_data():
    engine = _build_engine(min_edge=0.70, max_snapshot_age_seconds=30.0)
    snapshot = _build_snapshot(
        momentum=0.0040,
        market_price=99.6,
        reference_price=100.0,
        yes_pressure=80.0,
        no_pressure=20.0,
        age_seconds=120.0,
    )

    outcome = engine.decide(snapshot)

    assert outcome.action == 'no_trade'
    assert any(reason.startswith('stale_data:') for reason in outcome.blocked_reasons)


def test_duplicated_signal_guard_prevents_double_counting():
    engine = _build_engine(min_edge=0.80)
    snapshot = _build_snapshot(
        momentum=0.0040,
        market_price=99.6,
        reference_price=100.0,
        yes_pressure=50.0,
        no_pressure=50.0,
    )

    outcome = engine.decide(snapshot)

    assert outcome.action == 'no_trade'
    assert outcome.used_signals == ('momentum',)
    assert 'price_delta' in outcome.suppressed_signals
    assert any(reason.startswith('weak_edge:') for reason in outcome.blocked_reasons)


def test_risk_gate_blocks_when_health_or_balance_fails():
    engine = _build_engine(min_edge=0.70, min_balance_usdc=1.0)
    snapshot = _build_snapshot(
        momentum=0.0040,
        market_price=99.6,
        reference_price=100.0,
        yes_pressure=80.0,
        no_pressure=20.0,
        balance=None,
        health_state='failed',
    )

    outcome = engine.decide(snapshot)

    assert outcome.action == 'no_trade'
    assert 'missing_balance' in outcome.blocked_reasons
    assert 'health_state:failed' in outcome.blocked_reasons
