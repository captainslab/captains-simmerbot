from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from adapters.clob_auth_validator import AuthValidationResult
from engine.position_sizer import PositionSizer, PositionSizerConfig, PositionSizingResult, SizingBalanceSnapshot
from execution.readiness_gate import ReadinessGateConfig, ReadinessGateResult, evaluate_readiness
from execution.order_state_machine import OrderIntent


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    market_id: str
    action: str
    amount: float
    round_id: str | None = None
    feature_summary: dict[str, Any] = field(default_factory=dict)
    vote_summary: dict[str, Any] = field(default_factory=dict)
    edge: float = 0.0
    gate_result: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    final_action: str | None = None
    reasoning: str | None = None
    signal_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.round_id is None:
            object.__setattr__(self, 'round_id', self.decision_id)
        if self.final_action is None:
            object.__setattr__(self, 'final_action', self.action)


@dataclass(frozen=True)
class ExecutionEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    decision: DecisionRecord
    order_intent: OrderIntent | None = None
    sizing: PositionSizingResult | None = None
    auth: AuthValidationResult | None = None
    readiness: ReadinessGateResult | None = None
    outcome: str | None = None
    events: list[ExecutionEvent] = field(default_factory=list)
    _seen_broker_events: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.outcome in {'no_trade', 'filled', 'cancelled', 'rejected', 'failed'}


class Broker(Protocol):
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
    ) -> OrderIntent: ...


class TradeExecutor:
    def __init__(
        self,
        *,
        broker: Broker,
        position_sizer_config: PositionSizerConfig | None = None,
        readiness_gate_config: ReadinessGateConfig | None = None,
    ) -> None:
        self._broker = broker
        self._position_sizer = PositionSizer(config=position_sizer_config)
        derived_readiness = readiness_gate_config or ReadinessGateConfig(
            min_order_size=(position_sizer_config.min_size if position_sizer_config is not None else 1.0),
            max_balance_age_seconds=(position_sizer_config.max_balance_age_seconds if position_sizer_config is not None else 30.0),
        )
        self._readiness_gate_config = derived_readiness
        self._results: dict[str, ExecutionResult] = {}

    def get_result(self, decision_id: str) -> ExecutionResult | None:
        return self._results.get(decision_id)

    def execute(self, decision: DecisionRecord) -> ExecutionResult:
        existing = self._results.get(decision.decision_id)
        if existing is not None:
            self._emit(existing, 'duplicate_execution_ignored', decision_id=decision.decision_id)
            return existing

        result = ExecutionResult(decision=decision)
        self._results[decision.decision_id] = result
        self._emit(
            result,
            'execution_requested',
            decision_id=decision.decision_id,
            round_id=decision.round_id,
            action=decision.action,
            market_id=decision.market_id,
            amount=decision.amount,
            edge=decision.edge,
        )

        balance_snapshot = self._resolve_balance_snapshot(decision)
        result.auth = self._resolve_auth_result()
        if decision.action == 'no_trade':
            result.sizing = self._position_sizer.size(decision=decision, balance_snapshot=None)
            self._emit_auth(result)
            result.readiness = self._evaluate_readiness(decision, result.sizing, balance_snapshot, result.auth)
            self._emit_readiness(result)
            result.outcome = 'no_trade'
            self._emit(
                result,
                'no_trade_execution',
                decision_id=decision.decision_id,
                round_id=decision.round_id,
                market_id=decision.market_id,
            )
            self._emit(result, 'execution_terminal', outcome='no_trade')
            return result

        result.sizing = self._position_sizer.size(
            decision=decision,
            balance_snapshot=balance_snapshot,
        )
        self._emit_auth(result)
        result.readiness = self._evaluate_readiness(decision, result.sizing, balance_snapshot, result.auth)
        self._emit_readiness(result)
        if result.readiness.status == 'blocked':
            result.outcome = 'no_trade'
            self._emit(
                result,
                'execution_terminal',
                outcome='no_trade',
                reason=';'.join(result.readiness.reasons),
            )
            return result
        if result.readiness.status == 'ready_dry_run' and self._requested_mode() == 'live':
            result.outcome = 'no_trade'
            self._emit(
                result,
                'execution_terminal',
                outcome='no_trade',
                reason=';'.join(result.readiness.reasons or ('ready_dry_run',)),
            )
            return result

        side = self._resolve_side(decision.action)
        sized_signal_data = dict(decision.signal_data)
        sized_signal_data['order_size'] = result.sizing.size
        sized_signal_data['order_notional'] = result.sizing.notional
        self._emit(
            result,
            'broker_submit_requested',
            side=side,
            market_id=decision.market_id,
            amount=result.sizing.notional,
            size=result.sizing.size,
        )
        intent = self._broker.submit_order(
            idempotency_key=decision.decision_id,
            market_id=decision.market_id,
            side=side,
            amount=result.sizing.notional,
            reasoning=decision.reasoning,
            signal_data=sized_signal_data,
            readiness_status=result.readiness.status,
            auth_status=result.auth.status if result.auth is not None else None,
        )
        result.order_intent = intent
        self._append_broker_events(result)
        result.outcome = intent.state
        if intent.is_terminal:
            self._emit(
                result,
                'execution_terminal',
                outcome=intent.state,
                reason=intent.reason,
                provider_order_id=intent.provider_order_id,
            )
        return result

    def _append_broker_events(self, result: ExecutionResult) -> None:
        if result.order_intent is None:
            return
        new_events = result.order_intent.events[result._seen_broker_events :]
        for broker_event in new_events:
            self._emit(
                result,
                f'broker_{broker_event.state}',
                order_id=broker_event.order_id,
                **broker_event.details,
            )
        result._seen_broker_events = len(result.order_intent.events)

    @staticmethod
    def _resolve_side(action: str) -> str:
        if action == 'buy_yes':
            return 'yes'
        if action == 'buy_no':
            return 'no'
        raise ValueError(f'invalid_decision_action:{action}')

    def _resolve_balance_snapshot(self, decision: DecisionRecord) -> SizingBalanceSnapshot | None:
        fetch_balance = getattr(self._broker, 'fetch_balance', None)
        if callable(fetch_balance):
            snapshot = fetch_balance()
            return SizingBalanceSnapshot(
                available_usdc=getattr(snapshot, 'available_usdc', None),
                fetched_at=getattr(snapshot, 'fetched_at', None),
            )

        feature_summary = decision.feature_summary or {}
        if not feature_summary:
            return None
        return SizingBalanceSnapshot(
            available_usdc=feature_summary.get('available_balance_usdc'),
            fetched_at=feature_summary.get('balance_fetched_at') or feature_summary.get('observed_at'),
        )

    def _requested_mode(self) -> str:
        return str(getattr(self._broker, 'mode', 'dry_run') or 'dry_run')

    def _evaluate_readiness(
        self,
        decision: DecisionRecord,
        sizing: PositionSizingResult | None,
        balance_snapshot: SizingBalanceSnapshot | None,
        auth_result: AuthValidationResult | None,
    ) -> ReadinessGateResult:
        return evaluate_readiness(
            decision=decision,
            sizing=sizing,
            balance_snapshot=balance_snapshot,
            auth_result=auth_result or AuthValidationResult(status='auth_dry_run_only', reasons=('auth_not_validated',)),
            requested_mode=self._requested_mode(),
            live_trading_enabled=bool(getattr(self._broker, 'live_trading_enabled', False)),
            config=self._readiness_gate_config,
        )

    def _resolve_auth_result(self) -> AuthValidationResult:
        auth_result = getattr(self._broker, 'auth_result', None)
        if isinstance(auth_result, AuthValidationResult):
            return auth_result
        if bool(getattr(self._broker, 'auth_verified', False)):
            return AuthValidationResult(status='auth_ready', reasons=())
        return AuthValidationResult(status='auth_dry_run_only', reasons=('auth_not_validated',))

    def _emit_auth(self, result: ExecutionResult) -> None:
        auth = result.auth
        if auth is None:
            return
        self._emit(
            result,
            'auth_evaluated',
            status=auth.status,
            reasons=list(auth.reasons),
        )

    def _emit_readiness(self, result: ExecutionResult) -> None:
        readiness = result.readiness
        if readiness is None:
            return
        self._emit(
            result,
            'readiness_evaluated',
            status=readiness.status,
            reasons=list(readiness.reasons),
            requested_mode=self._requested_mode(),
        )

    @staticmethod
    def _emit(result: ExecutionResult, event_type: str, **details: Any) -> None:
        result.events.append(
            ExecutionEvent(
                event_type=event_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                details=details,
            )
        )
