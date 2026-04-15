from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from adapters.polymarket_clob import BalanceSnapshot, BrokerOrder
from execution.order_state_machine import OrderStateMachine
from execution.reconciliation import reconcile_trade
from scripts.reconcile_last_trade import run_reconcile_last_trade


def _balance(amount: float) -> BalanceSnapshot:
    return BalanceSnapshot(
        available_usdc=amount,
        total_exposure=0.0,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _payload(intent, broker_order, before_balance, after_balance) -> dict:
    return {
        'order_intent': {
            'idempotency_key': intent.idempotency_key,
            'market_id': intent.market_id,
            'side': intent.side,
            'amount': intent.amount,
            'state': intent.state,
            'provider_order_id': intent.provider_order_id,
            'filled_amount': intent.filled_amount,
            'remaining_amount': intent.remaining_amount,
            'reason': intent.reason,
            'balance_available': intent.balance_available,
            'events': [
                {
                    'order_id': event.order_id,
                    'state': event.state,
                    'timestamp': event.timestamp,
                    'details': event.details,
                }
                for event in intent.events
            ],
        },
        'broker_order': None
        if broker_order is None
        else {
            'order_id': broker_order.order_id,
            'market_id': broker_order.market_id,
            'side': broker_order.side,
            'amount': broker_order.amount,
            'status': broker_order.status,
            'filled_amount': broker_order.filled_amount,
            'remaining_amount': broker_order.remaining_amount,
            'average_price': broker_order.average_price,
            'reason': broker_order.reason,
        },
        'balance_before': {
            'available_usdc': before_balance.available_usdc,
            'total_exposure': before_balance.total_exposure,
            'fetched_at': before_balance.fetched_at,
        },
        'balance_after': {
            'available_usdc': after_balance.available_usdc,
            'total_exposure': after_balance.total_exposure,
            'fetched_at': after_balance.fetched_at,
        },
    }


def test_clean_filled_trade_reconciles():
    state_machine = OrderStateMachine()
    intent = state_machine.create_intent(idempotency_key='filled', market_id='m1', side='yes', amount=2.0)
    state_machine.transition(intent, 'submitted', provider_order_id='ord-1')
    state_machine.transition(intent, 'acknowledged', provider_order_id='ord-1')
    state_machine.transition(intent, 'filled', provider_order_id='ord-1', filled_amount=2.0, remaining_amount=0.0)
    broker_order = BrokerOrder(order_id='ord-1', market_id='m1', side='yes', amount=2.0, status='filled', filled_amount=2.0, remaining_amount=0.0)

    result = reconcile_trade(
        intent=intent,
        broker_order=broker_order,
        balance_before=_balance(10.0),
        balance_after=_balance(8.0),
    )

    assert result.status == 'reconciled'
    assert result.reasons == ()


def test_cancelled_rejected_trade_reconciles():
    for terminal_state in ('cancelled', 'rejected'):
        state_machine = OrderStateMachine()
        intent = state_machine.create_intent(idempotency_key=terminal_state, market_id='m1', side='yes', amount=2.0)
        state_machine.transition(intent, terminal_state, provider_order_id='ord-1', reason=terminal_state)
        broker_order = BrokerOrder(order_id='ord-1', market_id='m1', side='yes', amount=2.0, status=terminal_state, reason=terminal_state)

        result = reconcile_trade(
            intent=intent,
            broker_order=broker_order,
            balance_before=_balance(10.0),
            balance_after=_balance(10.0),
        )

        assert result.status == 'reconciled'


def test_fill_balance_mismatch_returns_mismatch():
    state_machine = OrderStateMachine()
    intent = state_machine.create_intent(idempotency_key='mismatch', market_id='m1', side='yes', amount=2.0)
    state_machine.transition(intent, 'submitted', provider_order_id='ord-1')
    state_machine.transition(intent, 'acknowledged', provider_order_id='ord-1')
    state_machine.transition(intent, 'filled', provider_order_id='ord-1', filled_amount=2.0, remaining_amount=0.0)
    broker_order = BrokerOrder(order_id='ord-1', market_id='m1', side='yes', amount=2.0, status='filled', filled_amount=2.0, remaining_amount=0.0)

    result = reconcile_trade(
        intent=intent,
        broker_order=broker_order,
        balance_before=_balance(10.0),
        balance_after=_balance(9.5),
    )

    assert result.status == 'mismatch'
    assert any(reason.startswith('unexpected_balance_delta:') for reason in result.reasons)


def test_missing_terminal_update_returns_unresolved():
    state_machine = OrderStateMachine()
    intent = state_machine.create_intent(idempotency_key='unresolved', market_id='m1', side='yes', amount=2.0)
    state_machine.transition(intent, 'submitted', provider_order_id='ord-1')
    state_machine.transition(intent, 'acknowledged', provider_order_id='ord-1')
    broker_order = BrokerOrder(order_id='ord-1', market_id='m1', side='yes', amount=2.0, status='acknowledged', filled_amount=0.0)

    result = reconcile_trade(
        intent=intent,
        broker_order=broker_order,
        balance_before=_balance(10.0),
        balance_after=_balance(10.0),
    )

    assert result.status == 'unresolved'
    assert 'missing_terminal_broker_update' in result.reasons


def test_reconciliation_never_submits_new_order(tmp_path: Path):
    state_machine = OrderStateMachine()
    intent = state_machine.create_intent(idempotency_key='script', market_id='m1', side='yes', amount=2.0)
    state_machine.transition(intent, 'submitted', provider_order_id='ord-1')
    state_machine.transition(intent, 'acknowledged', provider_order_id='ord-1')
    state_machine.transition(intent, 'cancelled', provider_order_id='ord-1', reason='user_cancel')
    broker_order = BrokerOrder(order_id='ord-1', market_id='m1', side='yes', amount=2.0, status='cancelled', reason='user_cancel')
    payload_path = tmp_path / 'reconcile.json'
    event_log = tmp_path / 'reconcile.jsonl'
    payload_path.write_text(json.dumps(_payload(intent, broker_order, _balance(10.0), _balance(10.0))))

    result = run_reconcile_last_trade(payload_path, event_log_path=event_log)

    assert result.status == 'reconciled'
    assert all('submit' not in event.event_type for event in result.events)
    assert len(event_log.read_text().splitlines()) == len(result.events)


def test_terminal_reconciliation_event_always_recorded():
    state_machine = OrderStateMachine()
    intent = state_machine.create_intent(idempotency_key='terminal', market_id='m1', side='yes', amount=2.0)
    state_machine.transition(intent, 'submitted', provider_order_id='ord-1')
    state_machine.transition(intent, 'acknowledged', provider_order_id='ord-1')
    broker_order = BrokerOrder(order_id='ord-1', market_id='m1', side='yes', amount=2.0, status='acknowledged', filled_amount=0.0)

    result = reconcile_trade(
        intent=intent,
        broker_order=broker_order,
        balance_before=_balance(10.0),
        balance_after=_balance(10.0),
    )

    assert result.events[-1].event_type == 'reconciliation_terminal'
