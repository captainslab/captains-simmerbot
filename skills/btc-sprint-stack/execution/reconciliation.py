from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from adapters.polymarket_clob import BalanceSnapshot, BrokerOrder
from execution.order_state_machine import OrderEvent, OrderIntent, TERMINAL_STATES


def _emit(events: list['ReconciliationEvent'], event_type: str, **details: Any) -> None:
    events.append(
        ReconciliationEvent(
            event_type=event_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            details=details,
        )
    )


@dataclass(frozen=True)
class ReconciliationEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    reasons: tuple[str, ...]
    balance_delta: float
    events: tuple[ReconciliationEvent, ...]


def balance_snapshot_from_dict(payload: dict[str, Any]) -> BalanceSnapshot:
    return BalanceSnapshot(
        available_usdc=float(payload['available_usdc']),
        total_exposure=float(payload.get('total_exposure', 0.0)),
        fetched_at=str(payload['fetched_at']),
    )


def broker_order_from_dict(payload: dict[str, Any]) -> BrokerOrder:
    return BrokerOrder(
        order_id=payload.get('order_id'),
        market_id=str(payload['market_id']),
        side=str(payload['side']),
        amount=float(payload['amount']),
        status=str(payload['status']),
        filled_amount=float(payload.get('filled_amount', 0.0)),
        remaining_amount=None if payload.get('remaining_amount') is None else float(payload['remaining_amount']),
        average_price=None if payload.get('average_price') is None else float(payload['average_price']),
        reason=payload.get('reason'),
    )


def order_intent_from_dict(payload: dict[str, Any]) -> OrderIntent:
    intent = OrderIntent(
        idempotency_key=str(payload['idempotency_key']),
        market_id=str(payload['market_id']),
        side=str(payload['side']),
        amount=float(payload['amount']),
        state=str(payload.get('state', 'created')),
        provider_order_id=payload.get('provider_order_id'),
        filled_amount=float(payload.get('filled_amount', 0.0)),
        remaining_amount=None if payload.get('remaining_amount') is None else float(payload['remaining_amount']),
        reason=payload.get('reason'),
        balance_available=None if payload.get('balance_available') is None else float(payload['balance_available']),
        events=[
            OrderEvent(
                order_id=event.get('order_id'),
                state=str(event['state']),
                timestamp=str(event['timestamp']),
                details=dict(event.get('details') or {}),
            )
            for event in payload.get('events', [])
        ],
    )
    return intent


def reconcile_trade(
    *,
    intent: OrderIntent,
    broker_order: BrokerOrder | None,
    balance_before: BalanceSnapshot,
    balance_after: BalanceSnapshot,
    tolerance: float = 0.01,
) -> ReconciliationResult:
    events: list[ReconciliationEvent] = []
    reasons: list[str] = []
    balance_delta = round(balance_before.available_usdc - balance_after.available_usdc, 4)

    _emit(
        events,
        'reconciliation_started',
        order_id=intent.provider_order_id,
        intent_state=intent.state,
        broker_status=(broker_order.status if broker_order is not None else None),
    )

    terminal_states = [event.state for event in intent.events if event.state in TERMINAL_STATES]
    if len(terminal_states) > 1:
        unique_terminal_states = set(terminal_states)
        if len(unique_terminal_states) > 1:
            reasons.append('conflicting_terminal_states')
        else:
            reasons.append(f'duplicate_terminal_state:{terminal_states[0]}')
    _emit(events, 'intent_reviewed', terminal_states=terminal_states, current_state=intent.state)

    if broker_order is None or broker_order.status not in TERMINAL_STATES:
        reasons.append('missing_terminal_broker_update')
        _emit(
            events,
            'broker_reviewed',
            broker_status=(broker_order.status if broker_order is not None else None),
            reasons=['missing_terminal_broker_update'],
        )
        result = ReconciliationResult(
            status='unresolved',
            reasons=tuple(reasons),
            balance_delta=balance_delta,
            events=(),
        )
        _emit(events, 'reconciliation_terminal', status=result.status, reasons=list(result.reasons))
        return ReconciliationResult(
            status=result.status,
            reasons=result.reasons,
            balance_delta=balance_delta,
            events=tuple(events),
        )

    if intent.state in TERMINAL_STATES and intent.state != broker_order.status:
        reasons.append(f'terminal_state_mismatch:{intent.state}!={broker_order.status}')

    if broker_order.status == 'filled':
        if broker_order.filled_amount <= 0 or intent.filled_amount != broker_order.filled_amount:
            reasons.append('fill_balance_mismatch')
        if abs(balance_delta - broker_order.amount) > tolerance:
            reasons.append(f'unexpected_balance_delta:{balance_delta:.4f}!={broker_order.amount:.4f}')
    elif broker_order.status in {'cancelled', 'rejected', 'failed'}:
        if abs(balance_delta) > tolerance:
            reasons.append(f'unexpected_balance_delta:{balance_delta:.4f}')

    _emit(
        events,
        'broker_reviewed',
        broker_status=broker_order.status,
        filled_amount=broker_order.filled_amount,
        reason=broker_order.reason,
    )
    _emit(
        events,
        'balance_reviewed',
        before=balance_before.available_usdc,
        after=balance_after.available_usdc,
        delta=balance_delta,
    )

    if reasons:
        status = 'mismatch'
    else:
        status = 'reconciled'

    _emit(events, 'reconciliation_terminal', status=status, reasons=list(reasons))
    return ReconciliationResult(
        status=status,
        reasons=tuple(reasons),
        balance_delta=balance_delta,
        events=tuple(events),
    )
