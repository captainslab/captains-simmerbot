from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from execution.session_controller import SessionEvent


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['SessionReportEvent'], event_type: str, **details: Any) -> None:
    events.append(
        SessionReportEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


def _as_reason_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    if value is None:
        return ()
    return (str(value),)


@dataclass(frozen=True)
class SessionReportEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionReport:
    session_id: str | None
    rounds_processed: int
    trades_attempted: int
    trades_skipped: int
    total_session_notional: float
    wins: int
    losses: int
    readiness_failure_count: int
    stop_reason: str | None
    reconciliation_status_summary: dict[str, int]
    final_session_verdict: str
    verdict_reasons: tuple[str, ...]
    events: tuple[SessionReportEvent, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


def session_event_from_dict(payload: dict[str, Any]) -> SessionEvent:
    return SessionEvent(
        event_type=str(payload['event_type']),
        timestamp=str(payload['timestamp']),
        details=dict(payload.get('details') or {}),
    )


def session_report_event_from_dict(payload: dict[str, Any]) -> SessionReportEvent:
    return SessionReportEvent(
        event_type=str(payload['event_type']),
        timestamp=str(payload['timestamp']),
        details=dict(payload.get('details') or {}),
    )


def session_report_from_dict(payload: dict[str, Any]) -> SessionReport:
    return SessionReport(
        session_id=None if payload.get('session_id') is None else str(payload['session_id']),
        rounds_processed=int(payload.get('rounds_processed', 0)),
        trades_attempted=int(payload.get('trades_attempted', 0)),
        trades_skipped=int(payload.get('trades_skipped', 0)),
        total_session_notional=float(payload.get('total_session_notional', 0.0)),
        wins=int(payload.get('wins', 0)),
        losses=int(payload.get('losses', 0)),
        readiness_failure_count=int(payload.get('readiness_failure_count', 0)),
        stop_reason=None if payload.get('stop_reason') is None else str(payload['stop_reason']),
        reconciliation_status_summary={
            'reconciled': int((payload.get('reconciliation_status_summary') or {}).get('reconciled', 0)),
            'mismatch': int((payload.get('reconciliation_status_summary') or {}).get('mismatch', 0)),
            'unresolved': int((payload.get('reconciliation_status_summary') or {}).get('unresolved', 0)),
            'unknown': int((payload.get('reconciliation_status_summary') or {}).get('unknown', 0)),
        },
        final_session_verdict=str(payload.get('final_session_verdict', 'blocked')),
        verdict_reasons=tuple(str(reason) for reason in payload.get('verdict_reasons', [])),
        events=tuple(
            session_report_event_from_dict(event)
            for event in payload.get('events', [])
        ),
    )


def load_session_events(path: Path) -> tuple[SessionEvent, ...]:
    events: list[SessionEvent] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        events.append(session_event_from_dict(json.loads(line)))
    return tuple(events)


def build_session_report(events: Iterable[SessionEvent]) -> SessionReport:
    normalized_events = tuple(events)
    report_events: list[SessionReportEvent] = []
    session_id = _find_session_id(normalized_events)
    rounds_processed = 0
    trades_attempted = 0
    trades_skipped = 0
    total_session_notional = 0.0
    wins = 0
    losses = 0
    readiness_skip_count = 0
    reconciliation_summary = {'reconciled': 0, 'mismatch': 0, 'unresolved': 0, 'unknown': 0}

    terminal_session_event = None
    for event in normalized_events:
        if event.event_type == 'session_stopped':
            terminal_session_event = event
        if event.event_type == 'trade_attempted':
            rounds_processed += 1
            trades_attempted += 1
            total_session_notional = round(
                total_session_notional + float(event.details.get('attempted_notional', 0.0)),
                4,
            )
            execution_outcome = str(event.details.get('execution_outcome') or '')
            reconciliation_status = str(event.details.get('reconciliation_status') or 'unknown')
            reconciliation_summary[reconciliation_status if reconciliation_status in reconciliation_summary else 'unknown'] += 1
            if execution_outcome == 'filled' and reconciliation_status == 'reconciled':
                wins += 1
            elif execution_outcome in {'cancelled', 'rejected', 'failed'}:
                losses += 1
        elif event.event_type == 'trade_skipped':
            rounds_processed += 1
            trades_skipped += 1
            reasons = _as_reason_list(event.details.get('reasons'))
            if any(_is_readiness_failure_reason(reason) for reason in reasons):
                readiness_skip_count += 1

    stop_reason = str(terminal_session_event.details.get('stop_reason')) if terminal_session_event is not None else None
    verdict_reasons: list[str] = []

    if terminal_session_event is None:
        verdict = 'blocked'
        verdict_reasons.append('missing_terminal_session_stopped')
    elif _is_blocking_stop_reason(stop_reason):
        verdict = 'blocked'
        verdict_reasons.append(stop_reason or 'blocking_stop_reason')
    else:
        if reconciliation_summary['mismatch'] > 0:
            verdict_reasons.append('reconciliation_mismatch_present')
        if reconciliation_summary['unresolved'] > 0:
            verdict_reasons.append('reconciliation_unresolved_present')
        if readiness_skip_count >= 2:
            verdict_reasons.append('repeated_readiness_skips')
        verdict = 'caution' if verdict_reasons else 'clean'

    _emit(
        report_events,
        'session_report_built',
        session_id=session_id,
        rounds_processed=rounds_processed,
        trades_attempted=trades_attempted,
        trades_skipped=trades_skipped,
        total_session_notional=total_session_notional,
        stop_reason=stop_reason,
    )
    _emit(
        report_events,
        'session_verdict_emitted',
        final_session_verdict=verdict,
        verdict_reasons=list(verdict_reasons),
    )
    return SessionReport(
        session_id=session_id,
        rounds_processed=rounds_processed,
        trades_attempted=trades_attempted,
        trades_skipped=trades_skipped,
        total_session_notional=total_session_notional,
        wins=wins,
        losses=losses,
        readiness_failure_count=readiness_skip_count,
        stop_reason=stop_reason,
        reconciliation_status_summary=reconciliation_summary,
        final_session_verdict=verdict,
        verdict_reasons=tuple(verdict_reasons),
        events=tuple(report_events),
    )


def _find_session_id(events: tuple[SessionEvent, ...]) -> str | None:
    for event in events:
        session_id = event.details.get('session_id')
        if session_id is not None:
            return str(session_id)
    return None


def _is_blocking_stop_reason(reason: str | None) -> bool:
    if not reason:
        return False
    if reason.startswith('reconciliation_'):
        return True
    return _is_readiness_failure_reason(reason)


def _is_readiness_failure_reason(reason: str) -> bool:
    return reason.startswith(
        (
            'stale_',
            'health_state:',
            'invalid_',
            'auth_',
            'session_not_verified',
            'live_trading_disabled',
            'missing_api_',
            'missing_signer',
            'missing_balance',
            'missing_market',
            'missing_timestamp',
            'below_minimum_size:',
        )
    ) or reason in {
        'missing_api_key',
        'missing_api_secret',
        'missing_api_passphrase',
        'missing_signer_address',
        'missing_signer_key',
        'auth_not_validated',
    }
