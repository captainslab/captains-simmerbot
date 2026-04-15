from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from adapters.clob_auth_validator import AuthValidationResult
from adapters.polymarket_clob import BalanceSnapshot, BrokerOrder
from execution.live_broker import LiveBroker, LiveBrokerConfig
from execution.order_state_machine import OrderStateMachine, TERMINAL_STATES


class StubAdapter:
    def __init__(
        self,
        *,
        balance: BalanceSnapshot | None = None,
        place_result: BrokerOrder | None = None,
        cancel_result: BrokerOrder | None = None,
        status_result: BrokerOrder | None = None,
    ) -> None:
        self.balance = balance or BalanceSnapshot(available_usdc=100.0, total_exposure=0.0, fetched_at='now')
        self.place_result = place_result
        self.cancel_result = cancel_result
        self.status_result = status_result
        self.fetch_balance_calls = 0
        self.place_order_calls = 0
        self.cancel_order_calls = 0
        self.fetch_order_status_calls = 0

    def fetch_balance(self) -> BalanceSnapshot:
        self.fetch_balance_calls += 1
        return self.balance

    def place_order(self, **_kwargs) -> BrokerOrder:
        self.place_order_calls += 1
        if self.place_result is None:
            raise AssertionError('place_order should not have been called')
        return self.place_result

    def cancel_order(self, _order_id: str) -> BrokerOrder:
        self.cancel_order_calls += 1
        if self.cancel_result is None:
            raise AssertionError('cancel_order should not have been called')
        return self.cancel_result

    def fetch_order_status(self, _order_id: str) -> BrokerOrder:
        self.fetch_order_status_calls += 1
        if self.status_result is None:
            raise AssertionError('fetch_order_status should not have been called')
        return self.status_result


def test_dry_run_happy_path():
    adapter = StubAdapter()
    broker = LiveBroker(adapter=adapter, config=LiveBrokerConfig(mode='dry_run'))

    intent = broker.submit_order(idempotency_key='abc', market_id='m1', side='yes', amount=4.0)

    assert intent.state == 'cancelled'
    assert intent.reason == 'dry_run_no_submit'
    assert adapter.fetch_balance_calls == 1
    assert adapter.place_order_calls == 0
    assert [event.state for event in intent.events] == ['created', 'cancelled']


def test_duplicate_submit_protection():
    adapter = StubAdapter()
    broker = LiveBroker(adapter=adapter, config=LiveBrokerConfig(mode='dry_run'))

    first = broker.submit_order(idempotency_key='same', market_id='m1', side='yes', amount=4.0)
    second = broker.submit_order(idempotency_key='same', market_id='m1', side='yes', amount=4.0)

    assert first is second
    assert adapter.fetch_balance_calls == 1
    assert adapter.place_order_calls == 0


def test_balance_mismatch_fail_no_trade():
    adapter = StubAdapter(balance=BalanceSnapshot(available_usdc=3.0, total_exposure=0.0, fetched_at='now'))
    broker = LiveBroker(adapter=adapter, config=LiveBrokerConfig(mode='dry_run'))

    intent = broker.submit_order(idempotency_key='low-balance', market_id='m1', side='yes', amount=4.0)

    assert intent.state == 'failed'
    assert 'insufficient_balance' in (intent.reason or '')
    assert adapter.place_order_calls == 0


def test_live_mode_blocked_without_auth():
    adapter = StubAdapter()
    broker = LiveBroker(
        adapter=adapter,
        config=LiveBrokerConfig(mode='live', live_trading_enabled=True, auth_verified=False),
    )

    intent = broker.submit_order(
        idempotency_key='blocked',
        market_id='m1',
        side='yes',
        amount=4.0,
        readiness_status='ready_live',
        auth_status='auth_dry_run_only',
    )

    assert intent.state == 'failed'
    assert intent.reason == 'live_auth_not_verified'
    assert adapter.place_order_calls == 0


def test_live_mode_verified_submit_acknowledged():
    adapter = StubAdapter(
        place_result=BrokerOrder(
            order_id='ord-1',
            market_id='m1',
            side='yes',
            amount=4.0,
            status='acknowledged',
        )
    )
    broker = LiveBroker(
        adapter=adapter,
        config=LiveBrokerConfig(
            mode='live',
            live_trading_enabled=True,
            auth_validation_result=AuthValidationResult(status='auth_ready', reasons=()),
        ),
    )

    intent = broker.submit_order(
        idempotency_key='live-ok',
        market_id='m1',
        side='yes',
        amount=4.0,
        readiness_status='ready_live',
        auth_status='auth_ready',
    )

    assert intent.state == 'acknowledged'
    assert intent.provider_order_id == 'ord-1'
    assert adapter.place_order_calls == 1
    assert [event.state for event in intent.events] == ['created', 'submitted', 'acknowledged']


def test_terminal_event_always_emitted():
    terminal_events = []
    state_machine = OrderStateMachine(event_sink=terminal_events.append)

    cancelled = state_machine.create_intent(idempotency_key='c', market_id='m', side='yes', amount=1.0)
    state_machine.transition(cancelled, 'cancelled', reason='dry_run')

    rejected = state_machine.create_intent(idempotency_key='r', market_id='m', side='yes', amount=1.0)
    state_machine.transition(rejected, 'rejected', reason='venue_rejected')

    failed = state_machine.create_intent(idempotency_key='f', market_id='m', side='yes', amount=1.0)
    state_machine.transition(failed, 'failed', reason='exception')

    filled = state_machine.create_intent(idempotency_key='x', market_id='m', side='yes', amount=1.0)
    state_machine.transition(filled, 'submitted', provider_order_id='o1')
    state_machine.transition(filled, 'acknowledged', provider_order_id='o1')
    state_machine.transition(filled, 'filled', provider_order_id='o1', filled_amount=1.0)

    observed_terminal_states = [event.state for event in terminal_events if event.state in TERMINAL_STATES]
    assert observed_terminal_states == ['cancelled', 'rejected', 'failed', 'filled']


def test_no_phantom_fill():
    adapter = StubAdapter(
        place_result=BrokerOrder(
            order_id='ord-1',
            market_id='m1',
            side='yes',
            amount=4.0,
            status='filled',
            filled_amount=0.0,
            remaining_amount=0.0,
        )
    )
    broker = LiveBroker(
        adapter=adapter,
        config=LiveBrokerConfig(
            mode='live',
            live_trading_enabled=True,
            auth_validation_result=AuthValidationResult(status='auth_ready', reasons=()),
        ),
    )

    intent = broker.submit_order(
        idempotency_key='phantom',
        market_id='m1',
        side='yes',
        amount=4.0,
        readiness_status='ready_live',
        auth_status='auth_ready',
    )

    assert intent.state == 'failed'
    assert intent.reason == 'phantom_fill_prevented'
    assert 'filled' not in [event.state for event in intent.events]
