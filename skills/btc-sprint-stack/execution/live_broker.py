from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adapters.clob_auth_validator import AuthValidationResult
from adapters.polymarket_clob import BrokerOrder, PolymarketClobAdapter
from execution.order_state_machine import OrderIntent, OrderStateMachine


@dataclass(frozen=True)
class LiveBrokerConfig:
    mode: str = 'dry_run'
    live_trading_enabled: bool = False
    auth_verified: bool = False
    auth_validation_result: AuthValidationResult | None = None
    order_type: str = 'FAK'
    source: str = 'btc_sprint_stack.live_broker'
    skill_slug: str = 'btc-sprint-stack'


class LiveBroker:
    def __init__(
        self,
        *,
        adapter: PolymarketClobAdapter,
        state_machine: OrderStateMachine | None = None,
        config: LiveBrokerConfig | None = None,
    ) -> None:
        self._adapter = adapter
        self._state_machine = state_machine or OrderStateMachine()
        self._config = config or LiveBrokerConfig()
        self._intents: dict[str, OrderIntent] = {}

    @property
    def mode(self) -> str:
        return self._config.mode

    @property
    def live_trading_enabled(self) -> bool:
        return self._config.live_trading_enabled

    @property
    def auth_verified(self) -> bool:
        return self.auth_result.status == 'auth_ready'

    @property
    def auth_result(self) -> AuthValidationResult:
        if self._config.auth_validation_result is not None:
            return self._config.auth_validation_result
        if self._config.auth_verified:
            return AuthValidationResult(status='auth_ready', reasons=())
        return AuthValidationResult(status='auth_dry_run_only', reasons=('live_auth_not_verified',))

    def fetch_balance(self):
        return self._adapter.fetch_balance()

    def submit_order(
        self,
        *,
        idempotency_key: str,
        market_id: str,
        side: str,
        amount: float,
        reasoning: str | None = None,
        signal_data: dict[str, Any] | None = None,
        readiness_status: str | None = None,
        auth_status: str | None = None,
    ) -> OrderIntent:
        if idempotency_key in self._intents:
            return self._intents[idempotency_key]

        intent = self._state_machine.create_intent(
            idempotency_key=idempotency_key,
            market_id=market_id,
            side=side,
            amount=amount,
        )
        self._intents[idempotency_key] = intent

        try:
            self._validate_request(side=side, amount=amount)
            balance = self._adapter.fetch_balance()
            if balance.available_usdc < amount:
                return self._state_machine.transition(
                    intent,
                    'failed',
                    reason=f'insufficient_balance:{balance.available_usdc:.4f}<{amount:.4f}',
                    balance_available=balance.available_usdc,
                )
            intent.balance_available = balance.available_usdc

            if self._config.mode == 'dry_run':
                return self._state_machine.transition(
                    intent,
                    'cancelled',
                    reason='dry_run_no_submit',
                    balance_available=balance.available_usdc,
                )

            self._ensure_live_ready()
            if readiness_status != 'ready_live':
                return self._state_machine.transition(
                    intent,
                    'failed',
                    reason=f'readiness_not_ready_live:{readiness_status or "missing"}',
                    balance_available=balance.available_usdc,
                )
            if auth_status != 'auth_ready':
                return self._state_machine.transition(
                    intent,
                    'failed',
                    reason=f'auth_not_ready:{auth_status or "missing"}',
                    balance_available=balance.available_usdc,
                )
            self._state_machine.transition(intent, 'submitted', balance_available=balance.available_usdc)
            broker_order = self._adapter.place_order(
                market_id=market_id,
                side=side,
                amount=amount,
                order_type=self._config.order_type,
                reasoning=reasoning,
                source=self._config.source,
                skill_slug=self._config.skill_slug,
                signal_data=signal_data,
            )
            return self._apply_broker_order(intent, broker_order)
        except Exception as exc:
            if not intent.is_terminal:
                self._state_machine.transition(intent, 'failed', reason=str(exc))
            return intent

    def cancel_order(self, *, idempotency_key: str) -> OrderIntent:
        intent = self._require_intent(idempotency_key)
        if intent.is_terminal:
            return intent
        if not intent.provider_order_id:
            return self._state_machine.transition(intent, 'failed', reason='missing_provider_order_id')
        broker_order = self._adapter.cancel_order(intent.provider_order_id)
        if broker_order.status != 'cancelled':
            return self._state_machine.transition(intent, 'failed', reason=broker_order.reason or broker_order.status)
        return self._state_machine.transition(
            intent,
            'cancelled',
            provider_order_id=broker_order.order_id,
            reason=broker_order.reason or 'cancelled',
        )

    def refresh_order_status(self, *, idempotency_key: str) -> OrderIntent:
        intent = self._require_intent(idempotency_key)
        if intent.is_terminal:
            return intent
        if not intent.provider_order_id:
            return self._state_machine.transition(intent, 'failed', reason='missing_provider_order_id')
        broker_order = self._adapter.fetch_order_status(intent.provider_order_id)
        return self._apply_broker_order(intent, broker_order)

    def _apply_broker_order(self, intent: OrderIntent, broker_order: BrokerOrder) -> OrderIntent:
        status = broker_order.status
        details = {
            'provider_order_id': broker_order.order_id,
            'filled_amount': broker_order.filled_amount,
            'remaining_amount': broker_order.remaining_amount,
            'reason': broker_order.reason,
        }
        if status == 'submitted':
            return self._state_machine.transition(intent, 'submitted', **details)
        if status == 'acknowledged':
            return self._state_machine.transition(intent, 'acknowledged', **details)
        if status == 'partially_filled':
            if broker_order.filled_amount <= 0:
                return self._state_machine.transition(intent, 'failed', reason='phantom_fill_prevented')
            return self._state_machine.transition(intent, 'partially_filled', **details)
        if status == 'filled':
            if broker_order.filled_amount <= 0:
                return self._state_machine.transition(intent, 'failed', reason='phantom_fill_prevented')
            if intent.state == 'created':
                self._state_machine.transition(intent, 'submitted', provider_order_id=broker_order.order_id)
            if intent.state == 'submitted':
                self._state_machine.transition(intent, 'acknowledged', provider_order_id=broker_order.order_id)
            return self._state_machine.transition(intent, 'filled', **details)
        if status == 'cancelled':
            return self._state_machine.transition(intent, 'cancelled', **details)
        if status == 'rejected':
            return self._state_machine.transition(intent, 'rejected', **details)
        return self._state_machine.transition(intent, 'failed', reason=broker_order.reason or status)

    def _ensure_live_ready(self) -> None:
        if self._config.mode != 'live':
            raise RuntimeError(f'unsupported_broker_mode:{self._config.mode}')
        if not self._config.live_trading_enabled:
            raise RuntimeError('live_trading_disabled')
        if self.auth_result.status != 'auth_ready':
            raise RuntimeError(self.auth_result.reasons[0] if self.auth_result.reasons else 'live_auth_not_verified')

    def _validate_request(self, *, side: str, amount: float) -> None:
        if side not in {'yes', 'no'}:
            raise RuntimeError(f'invalid_side:{side}')
        if amount <= 0:
            raise RuntimeError(f'invalid_amount:{amount}')

    def _require_intent(self, idempotency_key: str) -> OrderIntent:
        try:
            return self._intents[idempotency_key]
        except KeyError as exc:
            raise KeyError(f'unknown_idempotency_key:{idempotency_key}') from exc
