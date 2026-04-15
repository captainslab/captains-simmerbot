from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from execution.session_controller import SessionEvent
from reporting.performance_summary import build_performance_summary
from reporting.session_report import build_session_report
from scripts.evaluate_session_promotion import run_session_promotion_evaluation


def _session_event(event_type: str, **details) -> SessionEvent:
    return SessionEvent(
        event_type=event_type,
        timestamp=datetime.now(timezone.utc).isoformat(),
        details=details,
    )


def _session_report(
    *,
    session_id: str,
    attempted: list[tuple[float, str, str]],
    skipped_reasons: list[list[str]] | None = None,
    stop_reason: str = 'rounds_exhausted',
) -> dict:
    skipped_reasons = skipped_reasons or []
    events = [_session_event('session_started', session_id=session_id)]
    for index, (notional, outcome, reconciliation_status) in enumerate(attempted, start=1):
        events.append(
            _session_event(
                'trade_attempted',
                session_id=session_id,
                round_id=f'{session_id}:{index}',
                attempted_notional=notional,
                execution_outcome=outcome,
                reconciliation_status=reconciliation_status,
            )
        )
    for index, reasons in enumerate(skipped_reasons, start=1):
        events.append(
            _session_event(
                'trade_skipped',
                session_id=session_id,
                round_id=f'{session_id}:skip:{index}',
                reasons=reasons,
            )
        )
    events.append(_session_event('session_stopped', session_id=session_id, stop_reason=stop_reason))
    return build_session_report(events).as_dict()


def test_clean_multi_session_history_can_produce_eligible_for_review():
    summary = build_performance_summary(
        [
            _session_report(session_id='s1', attempted=[(1.0, 'filled', 'reconciled'), (1.0, 'filled', 'reconciled')]),
            _session_report(session_id='s2', attempted=[(1.5, 'filled', 'reconciled')]),
            _session_report(session_id='s3', attempted=[(1.0, 'filled', 'reconciled')]),
        ]
    )

    assert summary.promotion_verdict == 'eligible_for_review'
    assert summary.sessions_counted == 3
    assert summary.trades_counted == 4
    assert summary.win_loss_summary == {'wins': 4, 'losses': 0}
    assert summary.notional_summary == {'total': 4.5, 'average_per_session': 1.5}


def test_unresolved_mismatch_history_produces_blocked():
    unresolved = build_performance_summary(
        [
            _session_report(session_id='s1', attempted=[(1.0, 'filled', 'unresolved')], stop_reason='reconciliation_unresolved'),
        ]
    )
    repeated_mismatch = build_performance_summary(
        [
            _session_report(session_id='s1', attempted=[(1.0, 'filled', 'mismatch')]),
            _session_report(session_id='s2', attempted=[(1.0, 'filled', 'mismatch')]),
            _session_report(session_id='s3', attempted=[(1.0, 'filled', 'reconciled')]),
        ]
    )

    assert unresolved.promotion_verdict == 'blocked'
    assert 'unresolved_reconciliation_present' in unresolved.promotion_reason_codes
    assert repeated_mismatch.promotion_verdict == 'blocked'
    assert 'repeated_reconciliation_mismatch' in repeated_mismatch.promotion_reason_codes


def test_insufficient_sample_produces_stay_capped():
    summary = build_performance_summary(
        [
            _session_report(session_id='s1', attempted=[(1.0, 'filled', 'reconciled')]),
            _session_report(session_id='s2', attempted=[]),
        ]
    )

    assert summary.promotion_verdict == 'stay_capped'
    assert 'insufficient_sessions' in summary.promotion_reason_codes
    assert 'insufficient_trades' in summary.promotion_reason_codes
    assert 'insufficient_notional' in summary.promotion_reason_codes


def test_missing_terminal_session_report_produces_blocked():
    report = _session_report(session_id='s1', attempted=[(1.0, 'filled', 'reconciled')])
    report['events'] = report['events'][:-1]

    summary = build_performance_summary([report])

    assert summary.promotion_verdict == 'blocked'
    assert 'missing_terminal_session_report' in summary.promotion_reason_codes


def test_performance_summary_never_submits_or_mutates_anything(tmp_path: Path):
    report_path = tmp_path / 'session-report.json'
    payload = _session_report(session_id='s1', attempted=[(1.0, 'filled', 'reconciled'), (1.0, 'filled', 'reconciled')])
    original = json.dumps(payload, sort_keys=True)
    report_path.write_text(original, encoding='utf-8')

    result = run_session_promotion_evaluation([report_path])

    assert result['promotion_verdict'] == 'stay_capped'
    assert report_path.read_text(encoding='utf-8') == original


def test_promotion_verdict_is_always_emitted():
    summary = build_performance_summary([])

    assert summary.events[-1].event_type == 'promotion_verdict_emitted'
    assert summary.promotion_verdict == 'stay_capped'
