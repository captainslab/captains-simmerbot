from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from adapters.clob_auth_validator import AuthValidationResult
from adapters.polymarket_clob import BalanceSnapshot, BrokerOrder
from engine.decision_handoff import DecisionHandoff
from engine.feature_snapshot_builder import FeatureSnapshotInput, build_feature_snapshot
from engine.risk_gate import RiskGateConfig
from engine.vote_engine import VoteEngine, VoteEngineConfig
from execution.live_broker import LiveBroker, LiveBrokerConfig
from execution.order_state_machine import OrderStateMachine
from execution.trade_executor import TradeExecutor
from replay.runner import ReplayRunner


class StubAdapter:
    def __init__(self, *, balance: float = 100.0, place_result: BrokerOrder | None = None) -> None:
        self.balance = BalanceSnapshot(
            available_usdc=balance,
            total_exposure=0.0,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        self.place_result = place_result
        self.fetch_balance_calls = 0
        self.place_order_calls = 0

    def fetch_balance(self) -> BalanceSnapshot:
        self.fetch_balance_calls += 1
        return self.balance

    def place_order(self, **_kwargs) -> BrokerOrder:
        self.place_order_calls += 1
        if self.place_result is None:
            raise AssertionError('place_order should not have been called')
        return self.place_result


class RecordingBroker:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.state_machine = OrderStateMachine()
        self.balance = BalanceSnapshot(
            available_usdc=20.0,
            total_exposure=0.0,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        self.auth_result = AuthValidationResult(status='auth_ready', reasons=())

    def fetch_balance(self) -> BalanceSnapshot:
        return self.balance

    def submit_order(
        self,
        *,
        idempotency_key: str,
        market_id: str,
        side: str,
        amount: float,
        reasoning=None,
        signal_data=None,
        readiness_status=None,
        auth_status=None,
    ):
        del readiness_status, auth_status
        del reasoning, signal_data
        self.submit_calls += 1
        intent = self.state_machine.create_intent(
            idempotency_key=idempotency_key,
            market_id=market_id,
            side=side,
            amount=amount,
        )
        self.state_machine.transition(intent, 'submitted', provider_order_id=f'order-{self.submit_calls}')
        self.state_machine.transition(intent, 'acknowledged', provider_order_id=f'order-{self.submit_calls}')
        return intent


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


def _build_runner(*, broker, min_edge: float = 0.70, max_snapshot_age_seconds: float = 30.0) -> ReplayRunner:
    handoff = DecisionHandoff(
        vote_engine=VoteEngine(
            config=VoteEngineConfig(
                risk_gate=RiskGateConfig(
                    min_edge=min_edge,
                    max_snapshot_age_seconds=max_snapshot_age_seconds,
                    min_balance_usdc=1.0,
                    required_health_state='ok',
                )
            )
        )
    )
    return ReplayRunner(trade_executor=TradeExecutor(broker=broker), decision_handoff=handoff)


def test_buy_yes_decision_flows_through_replay_to_broker_intent():
    broker = RecordingBroker()
    runner = _build_runner(broker=broker)

    round_result = runner.run_round(
        _build_snapshot(
            momentum=0.0040,
            market_price=99.6,
            reference_price=100.0,
            yes_pressure=80.0,
            no_pressure=20.0,
        ),
        round_id='round-yes',
        trade_amount_usd=4.0,
    )

    assert round_result.decision.action == 'buy_yes'
    assert round_result.decision.round_id == 'round-yes'
    assert round_result.decision.gate_result['allowed'] is True
    assert round_result.execution is not None
    assert round_result.execution.order_intent is not None
    assert round_result.execution.order_intent.side == 'yes'
    assert broker.submit_calls == 1


def test_buy_no_decision_flows_through_replay_to_broker_intent():
    broker = RecordingBroker()
    runner = _build_runner(broker=broker)

    round_result = runner.run_round(
        _build_snapshot(
            momentum=-0.0040,
            market_price=100.4,
            reference_price=100.0,
            yes_pressure=20.0,
            no_pressure=80.0,
        ),
        round_id='round-no',
        trade_amount_usd=4.0,
    )

    assert round_result.decision.action == 'buy_no'
    assert round_result.execution is not None
    assert round_result.execution.order_intent is not None
    assert round_result.execution.order_intent.side == 'no'
    assert broker.submit_calls == 1


def test_no_trade_decision_emits_explicit_no_trade_recorded_and_no_broker_submit():
    broker = RecordingBroker()
    runner = _build_runner(broker=broker, min_edge=0.30)

    round_result = runner.run_round(
        _build_snapshot(
            momentum=0.0008,
            market_price=99.92,
            reference_price=100.0,
            yes_pressure=50.0,
            no_pressure=50.0,
        ),
        round_id='round-hold',
        trade_amount_usd=4.0,
    )

    assert round_result.decision.action == 'no_trade'
    assert broker.submit_calls == 0
    assert [event.event_type for event in round_result.events] == [
        'round_started',
        'no_trade_recorded',
        'execution_requested',
        'auth_evaluated',
        'readiness_evaluated',
        'no_trade_execution',
        'execution_terminal',
        'round_complete',
    ]


def test_weak_edge_or_stale_data_path_stays_blocked_end_to_end():
    broker = RecordingBroker()
    runner = _build_runner(broker=broker, min_edge=0.70, max_snapshot_age_seconds=30.0)

    round_result = runner.run_round(
        _build_snapshot(
            momentum=0.0040,
            market_price=99.6,
            reference_price=100.0,
            yes_pressure=80.0,
            no_pressure=20.0,
            age_seconds=120.0,
        ),
        round_id='round-stale',
        trade_amount_usd=4.0,
    )

    assert round_result.decision.action == 'no_trade'
    assert 'stale_data:120.0s' in round_result.decision.gate_result['reasons']
    assert broker.submit_calls == 0
    assert round_result.execution is not None
    assert round_result.execution.readiness is not None
    assert round_result.execution.readiness.status == 'blocked'


def test_event_ordering_remains_stable_for_live_capable_dry_run_round():
    adapter = StubAdapter()
    broker = LiveBroker(adapter=adapter, config=LiveBrokerConfig(mode='dry_run'))
    runner = _build_runner(broker=broker)

    round_result = runner.run_round(
        _build_snapshot(
            momentum=0.0040,
            market_price=99.6,
            reference_price=100.0,
            yes_pressure=80.0,
            no_pressure=20.0,
        ),
        round_id='round-dry',
        trade_amount_usd=4.0,
    )

    assert round_result.execution is not None
    assert round_result.execution.outcome == 'cancelled'
    assert [event.event_type for event in round_result.events] == [
        'round_started',
        'decision_recorded',
        'execution_requested',
        'auth_evaluated',
        'readiness_evaluated',
        'broker_submit_requested',
        'broker_created',
        'broker_cancelled',
        'execution_terminal',
        'round_complete',
    ]


def test_duplicate_execution_still_does_not_duplicate_submit():
    broker = RecordingBroker()
    runner = _build_runner(broker=broker)
    snapshot = _build_snapshot(
        momentum=0.0040,
        market_price=99.6,
        reference_price=100.0,
        yes_pressure=80.0,
        no_pressure=20.0,
    )

    first = runner.run_round(snapshot, round_id='round-dup', trade_amount_usd=4.0)
    second = runner.run_round(snapshot, round_id='round-dup', trade_amount_usd=4.0)

    assert broker.submit_calls == 1
    assert first.execution is second.execution
    assert second.execution is not None
    assert second.execution.events[-1].event_type == 'duplicate_execution_ignored'
