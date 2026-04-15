from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from adapters.polymarket_clob import BalanceSnapshot, BrokerOrder
from adapters.clob_auth_validator import AuthValidationResult
from engine.decision_handoff import DecisionHandoff
from engine.feature_snapshot_builder import FeatureSnapshotInput, build_feature_snapshot
from engine.position_sizer import PositionSizerConfig
from engine.risk_gate import RiskGateConfig
from engine.vote_engine import VoteEngine, VoteEngineConfig
from execution.live_broker import LiveBroker, LiveBrokerConfig
from execution.readiness_gate import ReadinessGateConfig, evaluate_readiness
from execution.trade_executor import DecisionRecord, TradeExecutor
from replay.runner import ReplayRunner


def _decision(**overrides) -> DecisionRecord:
    now = datetime.now(timezone.utc)
    payload = {
        'decision_id': 'round-1',
        'round_id': 'round-1',
        'market_id': 'btc-5m',
        'action': 'buy_yes',
        'final_action': 'buy_yes',
        'amount': 4.0,
        'edge': 1.0,
        'gate_result': {'allowed': True, 'reasons': []},
        'feature_summary': {
            'health_state': 'ok',
            'available_balance_usdc': 20.0,
            'observed_at': now.isoformat(),
        },
    }
    payload.update(overrides)
    return DecisionRecord(**payload)


def _balance(*, age_seconds: float = 0.0, amount: float | None = 20.0):
    return BalanceSnapshot(
        available_usdc=amount if amount is not None else 0.0,
        total_exposure=0.0,
        fetched_at=(datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
    )


class StubAdapter:
    def __init__(self, *, balance: BalanceSnapshot | None = None, place_result: BrokerOrder | None = None) -> None:
        self.balance = balance or _balance()
        self.place_result = place_result or BrokerOrder(
            order_id='ord-1',
            market_id='btc-5m',
            side='yes',
            amount=4.0,
            status='acknowledged',
        )
        self.place_order_calls = 0

    def fetch_balance(self) -> BalanceSnapshot:
        return self.balance

    def place_order(self, **_kwargs) -> BrokerOrder:
        self.place_order_calls += 1
        return self.place_result


def _build_runner(*, broker) -> ReplayRunner:
    handoff = DecisionHandoff(
        vote_engine=VoteEngine(
            config=VoteEngineConfig(
                risk_gate=RiskGateConfig(
                    min_edge=0.70,
                    max_snapshot_age_seconds=30.0,
                    min_balance_usdc=1.0,
                    required_health_state='ok',
                )
            )
        )
    )
    return ReplayRunner(
        trade_executor=TradeExecutor(
        broker=broker,
        position_sizer_config=PositionSizerConfig(
                min_size=1.0,
                max_size=4.0,
                max_balance_fraction=1.0,
                max_balance_age_seconds=30.0,
                min_edge=0.70,
            ),
            readiness_gate_config=ReadinessGateConfig(
                min_order_size=1.0,
                max_balance_age_seconds=30.0,
                max_market_age_seconds=30.0,
            ),
        ),
        decision_handoff=handoff,
    )


def _build_snapshot(*, age_seconds: float = 5.0):
    now = datetime.now(timezone.utc)
    return build_feature_snapshot(
        FeatureSnapshotInput(
            market_id='btc-5m',
            observed_at=now - timedelta(seconds=age_seconds),
            market_price=99.6,
            reference_price=100.0,
            momentum=0.0040,
            yes_pressure=80.0,
            no_pressure=20.0,
            available_balance_usdc=20.0,
            health_state='ok',
        ),
        now=now,
    )


def test_fully_ready_path_returns_ready_live():
    from engine.position_sizer import PositionSizingResult, SizingBalanceSnapshot

    readiness = evaluate_readiness(
        decision=_decision(),
        sizing=PositionSizingResult(allowed=True, size=4.0, notional=4.0, proposed_size=4.0, scale_factor=1.0, reasons=()),
        balance_snapshot=SizingBalanceSnapshot(available_usdc=20.0, fetched_at=datetime.now(timezone.utc).isoformat()),
        auth_result=AuthValidationResult(status='auth_ready', reasons=()),
        requested_mode='live',
        live_trading_enabled=True,
    )

    assert readiness.status == 'ready_live'
    assert readiness.reasons == ()


def test_auth_config_missing_returns_ready_dry_run_with_explicit_reason():
    from engine.position_sizer import PositionSizingResult, SizingBalanceSnapshot

    readiness = evaluate_readiness(
        decision=_decision(),
        sizing=PositionSizingResult(allowed=True, size=4.0, notional=4.0, proposed_size=4.0, scale_factor=1.0, reasons=()),
        balance_snapshot=SizingBalanceSnapshot(available_usdc=20.0, fetched_at=datetime.now(timezone.utc).isoformat()),
        auth_result=AuthValidationResult(status='auth_dry_run_only', reasons=('missing_api_key',)),
        requested_mode='live',
        live_trading_enabled=True,
    )

    assert readiness.status == 'ready_dry_run'
    assert readiness.reasons == ('missing_api_key',)


def test_stale_balance_returns_blocked():
    from engine.position_sizer import PositionSizingResult, SizingBalanceSnapshot

    readiness = evaluate_readiness(
        decision=_decision(),
        sizing=PositionSizingResult(allowed=True, size=4.0, notional=4.0, proposed_size=4.0, scale_factor=1.0, reasons=()),
        balance_snapshot=SizingBalanceSnapshot(
            available_usdc=20.0,
            fetched_at=(datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
        ),
        auth_result=AuthValidationResult(status='auth_ready', reasons=()),
        requested_mode='live',
        live_trading_enabled=True,
    )

    assert readiness.status == 'blocked'
    assert 'stale_balance:120.0s' in readiness.reasons


def test_stale_market_returns_blocked():
    from engine.position_sizer import PositionSizingResult, SizingBalanceSnapshot

    old = datetime.now(timezone.utc) - timedelta(seconds=120)
    readiness = evaluate_readiness(
        decision=_decision(feature_summary={'health_state': 'ok', 'available_balance_usdc': 20.0, 'observed_at': old.isoformat()}),
        sizing=PositionSizingResult(allowed=True, size=4.0, notional=4.0, proposed_size=4.0, scale_factor=1.0, reasons=()),
        balance_snapshot=SizingBalanceSnapshot(available_usdc=20.0, fetched_at=datetime.now(timezone.utc).isoformat()),
        auth_result=AuthValidationResult(status='auth_ready', reasons=()),
        requested_mode='live',
        live_trading_enabled=True,
    )

    assert readiness.status == 'blocked'
    assert 'stale_market:120.0s' in readiness.reasons


def test_invalid_action_or_zero_sub_min_size_returns_blocked():
    from engine.position_sizer import PositionSizingResult, SizingBalanceSnapshot

    invalid_action = evaluate_readiness(
        decision=_decision(action='hold', final_action='hold'),
        sizing=PositionSizingResult(allowed=False, size=0.0, notional=0.0, proposed_size=0.0, scale_factor=0.0, reasons=('invalid_sized_order',)),
        balance_snapshot=SizingBalanceSnapshot(available_usdc=20.0, fetched_at=datetime.now(timezone.utc).isoformat()),
        auth_result=AuthValidationResult(status='auth_ready', reasons=()),
        requested_mode='live',
        live_trading_enabled=True,
    )
    sub_min = evaluate_readiness(
        decision=_decision(),
        sizing=PositionSizingResult(
            allowed=False,
            size=0.0,
            notional=0.0,
            proposed_size=0.5,
            scale_factor=0.5,
            reasons=('below_minimum_size:0.5000<1.0000',),
        ),
        balance_snapshot=SizingBalanceSnapshot(available_usdc=20.0, fetched_at=datetime.now(timezone.utc).isoformat()),
        auth_result=AuthValidationResult(status='auth_ready', reasons=()),
        requested_mode='live',
        live_trading_enabled=True,
    )

    assert invalid_action.status == 'blocked'
    assert 'invalid_action:hold' in invalid_action.reasons
    assert sub_min.status == 'blocked'
    assert 'below_minimum_size:0.5000<1.0000' in sub_min.reasons


def test_event_log_records_readiness_result_before_submit_no_submit():
    dry_runner = _build_runner(
        broker=LiveBroker(
            adapter=StubAdapter(balance=_balance()),
            config=LiveBrokerConfig(mode='dry_run'),
        )
    )
    dry_result = dry_runner.run_round(_build_snapshot(), round_id='round-dry', trade_amount_usd=4.0)

    blocked_runner = _build_runner(
        broker=LiveBroker(
            adapter=StubAdapter(balance=_balance(age_seconds=120.0)),
            config=LiveBrokerConfig(mode='dry_run'),
        )
    )
    blocked_result = blocked_runner.run_round(_build_snapshot(), round_id='round-blocked', trade_amount_usd=4.0)

    dry_events = [event.event_type for event in dry_result.events]
    blocked_events = [event.event_type for event in blocked_result.events]
    assert dry_events.index('auth_evaluated') < dry_events.index('readiness_evaluated')
    assert dry_events.index('readiness_evaluated') < dry_events.index('broker_submit_requested')
    assert blocked_events.index('auth_evaluated') < blocked_events.index('readiness_evaluated')
    assert blocked_events.index('readiness_evaluated') < blocked_events.index('execution_terminal')


def test_live_submit_still_cannot_occur_when_readiness_is_not_ready_live():
    adapter = StubAdapter(balance=_balance(), place_result=BrokerOrder(order_id='ord-1', market_id='btc-5m', side='yes', amount=4.0, status='acknowledged'))
    runner = _build_runner(
        broker=LiveBroker(
            adapter=adapter,
            config=LiveBrokerConfig(
                mode='live',
                live_trading_enabled=True,
                auth_validation_result=AuthValidationResult(
                    status='auth_dry_run_only',
                    reasons=('missing_api_key',),
                ),
            ),
        )
    )

    round_result = runner.run_round(_build_snapshot(), round_id='round-live-blocked', trade_amount_usd=4.0)

    assert round_result.execution is not None
    assert round_result.execution.auth is not None
    assert round_result.execution.auth.status == 'auth_dry_run_only'
    assert round_result.execution.readiness is not None
    assert round_result.execution.readiness.status == 'ready_dry_run'
    assert 'missing_api_key' in round_result.execution.readiness.reasons
    assert adapter.place_order_calls == 0
