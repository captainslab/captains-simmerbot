from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from reporting.session_report import SessionReport, session_report_from_dict


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(events: list['PerformanceSummaryEvent'], event_type: str, **details: Any) -> None:
    events.append(
        PerformanceSummaryEvent(
            event_type=event_type,
            timestamp=_utc_now(),
            details=details,
        )
    )


@dataclass(frozen=True)
class PerformanceSummaryConfig:
    min_sessions_for_review: int = 3
    min_trades_for_review: int = 4
    min_notional_for_review: float = 4.0
    repeated_mismatch_threshold: int = 2


@dataclass(frozen=True)
class PerformanceSummaryEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PerformanceSummary:
    promotion_verdict: str
    sessions_counted: int
    trades_counted: int
    win_loss_summary: dict[str, int]
    notional_summary: dict[str, float]
    readiness_failure_count: int
    reconciliation_mismatch_count: int
    reconciliation_unresolved_count: int
    blocked_session_count: int
    caution_session_count: int
    promotion_reason_codes: tuple[str, ...]
    events: tuple[PerformanceSummaryEvent, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['events'] = [asdict(event) for event in self.events]
        return payload


def performance_summary_event_from_dict(payload: Mapping[str, Any]) -> PerformanceSummaryEvent:
    return PerformanceSummaryEvent(
        event_type=str(payload['event_type']),
        timestamp=str(payload['timestamp']),
        details=dict(payload.get('details') or {}),
    )


def performance_summary_from_dict(payload: Mapping[str, Any]) -> PerformanceSummary:
    return PerformanceSummary(
        promotion_verdict=str(payload.get('promotion_verdict', 'blocked')),
        sessions_counted=int(payload.get('sessions_counted', 0)),
        trades_counted=int(payload.get('trades_counted', 0)),
        win_loss_summary={
            'wins': int((payload.get('win_loss_summary') or {}).get('wins', 0)),
            'losses': int((payload.get('win_loss_summary') or {}).get('losses', 0)),
        },
        notional_summary={
            'total': float((payload.get('notional_summary') or {}).get('total', 0.0)),
            'average_per_session': float((payload.get('notional_summary') or {}).get('average_per_session', 0.0)),
        },
        readiness_failure_count=int(payload.get('readiness_failure_count', 0)),
        reconciliation_mismatch_count=int(payload.get('reconciliation_mismatch_count', 0)),
        reconciliation_unresolved_count=int(payload.get('reconciliation_unresolved_count', 0)),
        blocked_session_count=int(payload.get('blocked_session_count', 0)),
        caution_session_count=int(payload.get('caution_session_count', 0)),
        promotion_reason_codes=tuple(str(reason) for reason in payload.get('promotion_reason_codes', [])),
        events=tuple(
            performance_summary_event_from_dict(event)
            for event in payload.get('events', [])
        ),
    )


def load_session_reports(paths: Iterable[Path]) -> tuple[SessionReport, ...]:
    reports: list[SessionReport] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding='utf-8'))
        reports.append(session_report_from_dict(payload))
    return tuple(reports)


def build_performance_summary(
    reports: Iterable[SessionReport | Mapping[str, Any]],
    *,
    config: PerformanceSummaryConfig | None = None,
) -> PerformanceSummary:
    resolved = config or PerformanceSummaryConfig()
    normalized = tuple(_coerce_report(report) for report in reports)
    events: list[PerformanceSummaryEvent] = []

    sessions_counted = len(normalized)
    trades_counted = sum(report.trades_attempted for report in normalized)
    wins = sum(report.wins for report in normalized)
    losses = sum(report.losses for report in normalized)
    total_notional = round(sum(report.total_session_notional for report in normalized), 4)
    average_notional = round(total_notional / sessions_counted, 4) if sessions_counted else 0.0
    readiness_failure_count = sum(report.readiness_failure_count for report in normalized)
    reconciliation_mismatch_count = sum(report.reconciliation_status_summary.get('mismatch', 0) for report in normalized)
    reconciliation_unresolved_count = sum(report.reconciliation_status_summary.get('unresolved', 0) for report in normalized)
    blocked_session_count = sum(1 for report in normalized if report.final_session_verdict == 'blocked')
    caution_session_count = sum(1 for report in normalized if report.final_session_verdict == 'caution')
    missing_terminal_session_report_count = sum(
        1
        for report in normalized
        if not any(event.event_type == 'session_verdict_emitted' for event in report.events)
    )

    reason_codes: list[str] = []
    if reconciliation_unresolved_count > 0:
        reason_codes.append('unresolved_reconciliation_present')
    if reconciliation_mismatch_count >= resolved.repeated_mismatch_threshold:
        reason_codes.append('repeated_reconciliation_mismatch')
    if missing_terminal_session_report_count > 0:
        reason_codes.append('missing_terminal_session_report')

    if reason_codes:
        verdict = 'blocked'
    else:
        if sessions_counted < resolved.min_sessions_for_review:
            reason_codes.append('insufficient_sessions')
        if trades_counted < resolved.min_trades_for_review:
            reason_codes.append('insufficient_trades')
        if total_notional < resolved.min_notional_for_review:
            reason_codes.append('insufficient_notional')
        if readiness_failure_count > 0:
            reason_codes.append('readiness_failures_present')
        if blocked_session_count > 0:
            reason_codes.append('blocked_sessions_present')
        if caution_session_count > 0:
            reason_codes.append('caution_sessions_present')

        if reason_codes:
            verdict = 'stay_capped'
        else:
            verdict = 'eligible_for_review'
            reason_codes.append('sample_thresholds_met')

    _emit(
        events,
        'performance_summary_built',
        sessions_counted=sessions_counted,
        trades_counted=trades_counted,
        wins=wins,
        losses=losses,
        total_notional=total_notional,
        readiness_failure_count=readiness_failure_count,
        reconciliation_mismatch_count=reconciliation_mismatch_count,
        reconciliation_unresolved_count=reconciliation_unresolved_count,
    )
    _emit(
        events,
        'promotion_verdict_emitted',
        promotion_verdict=verdict,
        promotion_reason_codes=list(reason_codes),
    )
    return PerformanceSummary(
        promotion_verdict=verdict,
        sessions_counted=sessions_counted,
        trades_counted=trades_counted,
        win_loss_summary={'wins': wins, 'losses': losses},
        notional_summary={'total': total_notional, 'average_per_session': average_notional},
        readiness_failure_count=readiness_failure_count,
        reconciliation_mismatch_count=reconciliation_mismatch_count,
        reconciliation_unresolved_count=reconciliation_unresolved_count,
        blocked_session_count=blocked_session_count,
        caution_session_count=caution_session_count,
        promotion_reason_codes=tuple(reason_codes),
        events=tuple(events),
    )


def _coerce_report(report: SessionReport | Mapping[str, Any]) -> SessionReport:
    if isinstance(report, SessionReport):
        return report
    return session_report_from_dict(dict(report))
