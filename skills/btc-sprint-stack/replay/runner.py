from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from engine.feature_snapshot_builder import FeatureSnapshot
from execution.trade_executor import DecisionRecord, ExecutionResult, TradeExecutor


@dataclass(frozen=True)
class ReplayRoundEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayRoundResult:
    decision: DecisionRecord
    execution: ExecutionResult | None = None
    events: list[ReplayRoundEvent] = field(default_factory=list)


class DecisionBuilder(Protocol):
    def create_decision(
        self,
        *,
        round_id: str,
        snapshot: FeatureSnapshot,
        trade_amount_usd: float,
    ) -> DecisionRecord: ...


class ReplayRunner:
    def __init__(self, *, trade_executor: TradeExecutor, decision_handoff: DecisionBuilder | None = None) -> None:
        self._trade_executor = trade_executor
        self._decision_handoff = decision_handoff

    def run_round(
        self,
        snapshot_or_decision: FeatureSnapshot | DecisionRecord,
        *,
        round_id: str | None = None,
        trade_amount_usd: float | None = None,
    ) -> ReplayRoundResult:
        if isinstance(snapshot_or_decision, DecisionRecord):
            decision = snapshot_or_decision
            decision_event_type = 'decision_recorded'
            final_event_type = 'round_terminal'
        else:
            if self._decision_handoff is None:
                raise ValueError('decision_handoff_required')
            if not round_id:
                raise ValueError('round_id_required')
            if trade_amount_usd is None:
                raise ValueError('trade_amount_usd_required')
            decision = self._decision_handoff.create_decision(
                round_id=round_id,
                snapshot=snapshot_or_decision,
                trade_amount_usd=trade_amount_usd,
            )
            decision_event_type = 'no_trade_recorded' if decision.action == 'no_trade' else 'decision_recorded'
            final_event_type = 'round_complete'

        result = ReplayRoundResult(decision=decision)
        self._emit(result, 'round_started', decision_id=decision.decision_id, market_id=decision.market_id)
        self._emit(
            result,
            decision_event_type,
            decision_id=decision.decision_id,
            round_id=decision.round_id,
            action=decision.action,
            edge=decision.edge,
            gate_allowed=decision.gate_result.get('allowed'),
        )

        existing = self._trade_executor.get_result(decision.decision_id)
        seen = len(existing.events) if existing is not None else 0
        execution = self._trade_executor.execute(decision)
        result.execution = execution
        for event in execution.events[seen:]:
            self._emit(result, event.event_type, **event.details)

        self._emit(
            result,
            final_event_type,
            outcome=(execution.outcome or execution.order_intent.state) if execution.order_intent else execution.outcome,
            action=decision.action,
            readiness_status=(execution.readiness.status if execution.readiness is not None else None),
        )
        return result

    @staticmethod
    def _emit(result: ReplayRoundResult, event_type: str, **details: Any) -> None:
        result.events.append(
            ReplayRoundEvent(
                event_type=event_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                details=details,
            )
        )
