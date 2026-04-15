from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['SessionEvent'], event_type: str, **details: Any) -> None:
    events.append(
        SessionEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


@dataclass(frozen=True)
class SessionControllerConfig:
    max_trades_per_session: int
    max_notional_per_session: float
    max_consecutive_losses: int

    def __post_init__(self) -> None:
        if self.max_trades_per_session <= 0:
            raise ValueError('invalid_max_trades_per_session')
        if self.max_notional_per_session <= 0:
            raise ValueError('invalid_max_notional_per_session')
        if self.max_consecutive_losses <= 0:
            raise ValueError('invalid_max_consecutive_losses')


@dataclass(frozen=True)
class SessionRoundSpec:
    round_id: str
    requested_notional: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionRoundOutcome:
    session_action: str
    attempted_notional: float = 0.0
    round_status: str | None = None
    execution_outcome: str | None = None
    reasons: tuple[str, ...] = ()
    reconciliation_status: str | None = None
    reconciliation_reasons: tuple[str, ...] = ()
    stop_reason: str | None = None
    loss: bool = False

    def __post_init__(self) -> None:
        if self.session_action not in {'trade_attempted', 'trade_skipped'}:
            raise ValueError(f'invalid_session_action:{self.session_action}')
        if self.attempted_notional < 0:
            raise ValueError('invalid_attempted_notional')


@dataclass(frozen=True)
class SessionEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionResult:
    status: str
    stop_reason: str
    trades_attempted: int
    total_notional: float
    consecutive_losses: int
    events: tuple[SessionEvent, ...]


class SessionRoundProcessor(Protocol):
    def process_round(self, round_spec: SessionRoundSpec) -> SessionRoundOutcome: ...


class SessionController:
    def __init__(
        self,
        *,
        config: SessionControllerConfig,
        round_processor: SessionRoundProcessor,
    ) -> None:
        self._config = config
        self._round_processor = round_processor

    def run(
        self,
        rounds: list[SessionRoundSpec],
        *,
        session_id: str,
    ) -> SessionResult:
        events: list[SessionEvent] = []
        trades_attempted = 0
        total_notional = 0.0
        consecutive_losses = 0

        _emit(
            events,
            'session_started',
            session_id=session_id,
            max_trades_per_session=self._config.max_trades_per_session,
            max_notional_per_session=self._config.max_notional_per_session,
            max_consecutive_losses=self._config.max_consecutive_losses,
            planned_rounds=len(rounds),
        )

        stop_reason = 'rounds_exhausted'
        for index, round_spec in enumerate(rounds, start=1):
            if trades_attempted >= self._config.max_trades_per_session:
                stop_reason = 'max_trades_per_session_reached'
                break
            if total_notional >= self._config.max_notional_per_session:
                stop_reason = 'max_notional_per_session_reached'
                break
            if total_notional + round_spec.requested_notional > self._config.max_notional_per_session:
                stop_reason = 'max_notional_per_session_cap_reached'
                break
            if consecutive_losses >= self._config.max_consecutive_losses:
                stop_reason = 'max_consecutive_losses_reached'
                break

            outcome = self._round_processor.process_round(round_spec)
            if outcome.session_action == 'trade_attempted':
                trades_attempted += 1
                total_notional = round(total_notional + outcome.attempted_notional, 4)
                consecutive_losses = consecutive_losses + 1 if outcome.loss else 0
                _emit(
                    events,
                    'trade_attempted',
                    session_id=session_id,
                    round_id=round_spec.round_id,
                    round_index=index,
                    attempted_notional=outcome.attempted_notional,
                    round_status=outcome.round_status,
                    execution_outcome=outcome.execution_outcome,
                    reasons=list(outcome.reasons),
                    reconciliation_status=outcome.reconciliation_status,
                    reconciliation_reasons=list(outcome.reconciliation_reasons),
                    stop_reason=outcome.stop_reason,
                    trades_attempted=trades_attempted,
                    total_notional=total_notional,
                    consecutive_losses=consecutive_losses,
                )
            else:
                _emit(
                    events,
                    'trade_skipped',
                    session_id=session_id,
                    round_id=round_spec.round_id,
                    round_index=index,
                    requested_notional=round_spec.requested_notional,
                    round_status=outcome.round_status,
                    reasons=list(outcome.reasons),
                    stop_reason=outcome.stop_reason,
                    trades_attempted=trades_attempted,
                    total_notional=total_notional,
                    consecutive_losses=consecutive_losses,
                )

            if outcome.stop_reason is not None:
                stop_reason = outcome.stop_reason
                break
            if trades_attempted >= self._config.max_trades_per_session:
                stop_reason = 'max_trades_per_session_reached'
                break
            if total_notional >= self._config.max_notional_per_session:
                stop_reason = 'max_notional_per_session_reached'
                break
            if consecutive_losses >= self._config.max_consecutive_losses:
                stop_reason = 'max_consecutive_losses_reached'
                break

        _emit(
            events,
            'session_stopped',
            session_id=session_id,
            stop_reason=stop_reason,
            trades_attempted=trades_attempted,
            total_notional=total_notional,
            consecutive_losses=consecutive_losses,
        )
        return SessionResult(
            status='session_stopped',
            stop_reason=stop_reason,
            trades_attempted=trades_attempted,
            total_notional=total_notional,
            consecutive_losses=consecutive_losses,
            events=tuple(events),
        )
