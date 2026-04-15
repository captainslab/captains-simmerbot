from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from adapters.clob_auth_validator import AuthValidationResult
from adapters.polymarket_clob import BalanceSnapshot, BrokerOrder
from execution.live_broker import LiveBroker, LiveBrokerConfig
from execution.order_state_machine import OrderStateMachine
from execution.trade_executor import DecisionRecord, TradeExecutor
from replay.runner import ReplayRunner


def _decision(**overrides):
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        'decision_id': 'd',
        'market_id': 'm1',
        'action': 'buy_yes',
        'amount': 4.0,
        'edge': 1.0,
        'gate_result': {'allowed': True, 'reasons': []},
        'feature_summary': {
            'available_balance_usdc': 20.0,
            'observed_at': now,
            'health_state': 'ok',
        },
    }
    payload.update(overrides)
    return DecisionRecord(**payload)


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
        del reasoning, signal_data, readiness_status, auth_status
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


def test_no_trade_decision_emits_no_trade_execution_and_no_broker_submit():
    broker = RecordingBroker()
    executor = TradeExecutor(broker=broker)

    result = executor.execute(_decision(decision_id='d1', action='no_trade'))

    assert broker.submit_calls == 0
    assert result.outcome == 'no_trade'
    assert result.sizing is not None
    assert result.sizing.size == 0.0
    assert [event.event_type for event in result.events] == [
        'execution_requested',
        'auth_evaluated',
        'readiness_evaluated',
        'no_trade_execution',
        'execution_terminal',
    ]


def test_buy_yes_decision_submits_one_order_intent_only_once():
    broker = RecordingBroker()
    executor = TradeExecutor(broker=broker)

    result = executor.execute(_decision(decision_id='yes1', action='buy_yes'))

    assert broker.submit_calls == 1
    assert result.sizing is not None
    assert result.sizing.notional == 4.0
    assert result.order_intent is not None
    assert result.order_intent.side == 'yes'


def test_buy_no_decision_submits_one_order_intent_only_once():
    broker = RecordingBroker()
    executor = TradeExecutor(broker=broker)

    result = executor.execute(_decision(decision_id='no1', action='buy_no'))

    assert broker.submit_calls == 1
    assert result.order_intent is not None
    assert result.order_intent.side == 'no'


def test_duplicate_execution_call_does_not_duplicate_submit():
    broker = RecordingBroker()
    executor = TradeExecutor(broker=broker)
    decision = _decision(decision_id='dup', action='buy_yes')

    first = executor.execute(decision)
    second = executor.execute(decision)

    assert first is second
    assert broker.submit_calls == 1
    assert second.events[-1].event_type == 'duplicate_execution_ignored'


def test_dry_run_path_emits_execution_events_and_terminal_outcome():
    adapter = StubAdapter()
    broker = LiveBroker(adapter=adapter, config=LiveBrokerConfig(mode='dry_run'))
    executor = TradeExecutor(broker=broker)
    runner = ReplayRunner(trade_executor=executor)

    round_result = runner.run_round(_decision(decision_id='dry', action='buy_yes'))

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
        'round_terminal',
    ]


def test_live_path_is_blocked_when_auth_config_not_verified():
    adapter = StubAdapter(place_result=BrokerOrder(order_id='o1', market_id='m1', side='yes', amount=4.0, status='acknowledged'))
    broker = LiveBroker(adapter=adapter, config=LiveBrokerConfig(mode='live', live_trading_enabled=True, auth_verified=False))
    executor = TradeExecutor(broker=broker)
    runner = ReplayRunner(trade_executor=executor)

    round_result = runner.run_round(_decision(decision_id='live-block', action='buy_yes'))

    assert round_result.execution.outcome == 'no_trade'
    assert round_result.execution.order_intent is None
    assert round_result.execution.auth is not None
    assert round_result.execution.auth.status == 'auth_dry_run_only'
    assert round_result.execution.readiness is not None
    assert round_result.execution.readiness.status == 'ready_dry_run'
    assert 'live_auth_not_verified' in round_result.execution.readiness.reasons
    assert adapter.place_order_calls == 0
