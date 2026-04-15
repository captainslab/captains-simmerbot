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
from reporting.session_report import build_session_report
from scripts.report_last_session import run_report_last_session


def _event(event_type: str, **details) -> SessionEvent:
    return SessionEvent(
        event_type=event_type,
        timestamp=datetime.now(timezone.utc).isoformat(),
        details=details,
    )


def _write_event_log(path: Path, events: list[SessionEvent]) -> str:
    payload = '\n'.join(
        json.dumps(
            {
                'event_type': event.event_type,
                'timestamp': event.timestamp,
                'details': event.details,
            },
            sort_keys=True,
        )
        for event in events
    )
    path.write_text(payload, encoding='utf-8')
    return payload


def test_clean_session_reports_clean():
    report = build_session_report(
        [
            _event('session_started', session_id='s1'),
            _event(
                'trade_attempted',
                session_id='s1',
                round_id='r1',
                attempted_notional=1.5,
                execution_outcome='filled',
                reconciliation_status='reconciled',
            ),
            _event(
                'trade_skipped',
                session_id='s1',
                round_id='r2',
                reasons=['no_trade_action'],
            ),
            _event(
                'session_stopped',
                session_id='s1',
                stop_reason='rounds_exhausted',
                total_notional=1.5,
            ),
        ]
    )

    assert report.session_id == 's1'
    assert report.rounds_processed == 2
    assert report.trades_attempted == 1
    assert report.trades_skipped == 1
    assert report.total_session_notional == 1.5
    assert report.wins == 1
    assert report.losses == 0
    assert report.stop_reason == 'rounds_exhausted'
    assert report.reconciliation_status_summary == {
        'reconciled': 1,
        'mismatch': 0,
        'unresolved': 0,
        'unknown': 0,
    }
    assert report.final_session_verdict == 'clean'


def test_mismatch_unresolved_session_reports_caution_or_blocked_correctly():
    caution_report = build_session_report(
        [
            _event('session_started', session_id='s1'),
            _event(
                'trade_attempted',
                session_id='s1',
                round_id='r1',
                attempted_notional=1.0,
                execution_outcome='filled',
                reconciliation_status='mismatch',
            ),
            _event('session_stopped', session_id='s1', stop_reason='rounds_exhausted'),
        ]
    )
    blocked_report = build_session_report(
        [
            _event('session_started', session_id='s2'),
            _event(
                'trade_attempted',
                session_id='s2',
                round_id='r1',
                attempted_notional=1.0,
                execution_outcome='filled',
                reconciliation_status='unresolved',
            ),
            _event('session_stopped', session_id='s2', stop_reason='reconciliation_unresolved'),
        ]
    )

    assert caution_report.final_session_verdict == 'caution'
    assert 'reconciliation_mismatch_present' in caution_report.verdict_reasons
    assert blocked_report.final_session_verdict == 'blocked'
    assert blocked_report.stop_reason == 'reconciliation_unresolved'


def test_missing_terminal_session_event_reports_blocked():
    report = build_session_report(
        [
            _event('session_started', session_id='s1'),
            _event(
                'trade_attempted',
                session_id='s1',
                round_id='r1',
                attempted_notional=1.0,
                execution_outcome='filled',
                reconciliation_status='reconciled',
            ),
        ]
    )

    assert report.final_session_verdict == 'blocked'
    assert report.verdict_reasons == ('missing_terminal_session_stopped',)


def test_reporting_never_submits_or_mutates_anything(tmp_path: Path):
    event_log = tmp_path / 'session.jsonl'
    original = _write_event_log(
        event_log,
        [
            _event('session_started', session_id='s1'),
            _event(
                'trade_attempted',
                session_id='s1',
                round_id='r1',
                attempted_notional=1.0,
                execution_outcome='filled',
                reconciliation_status='reconciled',
            ),
            _event('session_stopped', session_id='s1', stop_reason='rounds_exhausted'),
        ],
    )

    report = run_report_last_session(event_log)

    assert report['final_session_verdict'] == 'clean'
    assert event_log.read_text(encoding='utf-8') == original


def test_repeated_readiness_skip_session_reports_caution():
    report = build_session_report(
        [
            _event('session_started', session_id='s1'),
            _event('trade_skipped', session_id='s1', round_id='r1', reasons=['stale_market:61.0s']),
            _event('trade_skipped', session_id='s1', round_id='r2', reasons=['missing_api_key']),
            _event('session_stopped', session_id='s1', stop_reason='rounds_exhausted'),
        ]
    )

    assert report.final_session_verdict == 'caution'
    assert 'repeated_readiness_skips' in report.verdict_reasons


def test_final_session_verdict_is_always_emitted():
    report = build_session_report(
        [
            _event('session_started', session_id='s1'),
            _event('session_stopped', session_id='s1', stop_reason='health_state:failed'),
        ]
    )

    assert report.events[-1].event_type == 'session_verdict_emitted'
    assert report.final_session_verdict == 'blocked'
