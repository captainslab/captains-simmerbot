from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


TERMINAL_STATES = {'filled', 'cancelled', 'rejected', 'failed'}
VALID_STATES = TERMINAL_STATES | {'created', 'submitted', 'acknowledged', 'partially_filled'}
ALLOWED_TRANSITIONS = {
    'created': {'submitted', 'cancelled', 'rejected', 'failed'},
    'submitted': {'acknowledged', 'partially_filled', 'filled', 'cancelled', 'rejected', 'failed'},
    'acknowledged': {'partially_filled', 'filled', 'cancelled', 'rejected', 'failed'},
    'partially_filled': {'filled', 'cancelled', 'failed'},
    'filled': set(),
    'cancelled': set(),
    'rejected': set(),
    'failed': set(),
}


@dataclass(frozen=True)
class OrderEvent:
    order_id: str | None
    state: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderIntent:
    idempotency_key: str
    market_id: str
    side: str
    amount: float
    state: str = 'created'
    provider_order_id: str | None = None
    filled_amount: float = 0.0
    remaining_amount: float | None = None
    reason: str | None = None
    balance_available: float | None = None
    events: list[OrderEvent] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class OrderStateMachine:
    def __init__(self, *, event_sink: Callable[[OrderEvent], None] | None = None) -> None:
        self._event_sink = event_sink

    def create_intent(self, *, idempotency_key: str, market_id: str, side: str, amount: float) -> OrderIntent:
        intent = OrderIntent(
            idempotency_key=idempotency_key,
            market_id=market_id,
            side=side,
            amount=amount,
        )
        self._emit(intent, 'created', idempotency_key=idempotency_key, market_id=market_id, side=side, amount=amount)
        return intent

    def transition(self, intent: OrderIntent, new_state: str, **details: Any) -> OrderIntent:
        if new_state not in VALID_STATES:
            raise ValueError(f'invalid_order_state:{new_state}')
        if new_state not in ALLOWED_TRANSITIONS[intent.state]:
            raise ValueError(f'invalid_order_transition:{intent.state}->{new_state}')
        if 'provider_order_id' in details and details['provider_order_id'] is not None:
            intent.provider_order_id = str(details['provider_order_id'])
        if 'filled_amount' in details and details['filled_amount'] is not None:
            intent.filled_amount = float(details['filled_amount'])
        if 'remaining_amount' in details:
            intent.remaining_amount = None if details['remaining_amount'] is None else float(details['remaining_amount'])
        if 'reason' in details and details['reason'] is not None:
            intent.reason = str(details['reason'])
        if 'balance_available' in details and details['balance_available'] is not None:
            intent.balance_available = float(details['balance_available'])
        intent.state = new_state
        self._emit(intent, new_state, **details)
        return intent

    def _emit(self, intent: OrderIntent, state: str, **details: Any) -> None:
        event = OrderEvent(
            order_id=intent.provider_order_id,
            state=state,
            timestamp=datetime.now(timezone.utc).isoformat(),
            details=details,
        )
        intent.events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)
