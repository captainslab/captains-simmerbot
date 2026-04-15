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
from scripts.run_end_to_end_smoke import run_end_to_end_smoke


def _session_event(event_type: str, **details) -> SessionEvent:
    return SessionEvent(
        event_type=event_type,
        timestamp=datetime.now(timezone.utc).isoformat(),
        details=details,
    )


def _write_file(path: Path, content: str = '#!/usr/bin/env python3\n') -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path.as_posix()


def _prereqs(tmp_path: Path, *, include_live: bool = True) -> dict:
    runtime_launcher = _write_file(tmp_path / 'scripts' / 'run_active_mode.py')
    session_report = _write_file(tmp_path / 'scripts' / 'report_last_session.py')
    reconcile = _write_file(tmp_path / 'scripts' / 'reconcile_last_trade.py')
    readiness_probe = _write_file(tmp_path / 'scripts' / 'prove_live_ready.py') if include_live else (tmp_path / 'scripts' / 'missing_probe.py').as_posix()
    readiness_config = _write_file(tmp_path / 'config' / 'live_probe.json', '{}') if include_live else (tmp_path / 'config' / 'missing_probe.json').as_posix()
    first_live = _write_file(tmp_path / 'scripts' / 'run_first_live_trade.py') if include_live else (tmp_path / 'scripts' / 'missing_first_live.py').as_posix()
    session_log = (tmp_path / 'artifacts' / 'session-events.jsonl').as_posix()
    session_report_output = (tmp_path / 'artifacts' / 'session-report.json').as_posix()
    reconciliation_payload = (tmp_path / 'artifacts' / 'reconcile.json').as_posix()
    Path(session_log).parent.mkdir(parents=True, exist_ok=True)
    return {
        'runtime_launcher_script_path': runtime_launcher,
        'readiness_probe_script_path': readiness_probe,
        'readiness_probe_config_path': readiness_config,
        'first_live_trade_script_path': first_live,
        'reconcile_last_trade_script_path': reconcile,
        'session_report_script_path': session_report,
        'session_event_log_path': session_log,
        'session_report_output_path': session_report_output,
        'reconciliation_payload_path': reconciliation_payload,
    }


def _reconciliation_payload(status: str = 'cancelled') -> dict:
    return {
        'order_intent': {
            'idempotency_key': 'smoke-order-1',
            'market_id': 'btc-5m',
            'side': 'yes',
            'amount': 1.0,
            'state': status,
            'provider_order_id': 'smoke-ord-1',
            'filled_amount': 0.0 if status == 'cancelled' else 1.0,
            'remaining_amount': 1.0 if status == 'cancelled' else 0.0,
            'reason': 'dry_run_no_submit',
            'balance_available': 10.0,
            'events': [
                {
                    'order_id': None,
                    'state': 'created',
                    'timestamp': '2026-04-14T20:00:00Z',
                    'details': {
                        'idempotency_key': 'smoke-order-1',
                        'market_id': 'btc-5m',
                        'side': 'yes',
                        'amount': 1.0,
                    },
                },
                {
                    'order_id': 'smoke-ord-1',
                    'state': status,
                    'timestamp': '2026-04-14T20:00:01Z',
                    'details': {
                        'reason': 'dry_run_no_submit',
                        'balance_available': 10.0,
                    },
                },
            ],
        },
        'broker_order': {
            'order_id': 'smoke-ord-1',
            'market_id': 'btc-5m',
            'side': 'yes',
            'amount': 1.0,
            'status': status,
            'filled_amount': 0.0 if status == 'cancelled' else 1.0,
            'remaining_amount': 1.0 if status == 'cancelled' else 0.0,
            'average_price': None,
            'reason': 'dry_run_no_submit',
        },
        'balance_before': {
            'available_usdc': 10.0,
            'total_exposure': 0.0,
            'fetched_at': '2026-04-14T20:00:00Z',
        },
        'balance_after': {
            'available_usdc': 10.0,
            'total_exposure': 0.0,
            'fetched_at': '2026-04-14T20:00:02Z',
        },
    }


def _profile(tmp_path: Path, *, mode: str = 'dry_run', include_live: bool = True, reconciliation_status: str = 'cancelled') -> Path:
    payload = {
        'deployment_mode': {
            'mode': mode,
            'profile': None,
            'reasons': [f'{mode}_enabled'],
        },
        'active_profile': {
            'profile_name': 'capped-safe-v1',
            'max_trades_per_session': 2,
            'max_notional_per_session': 5.0,
            'max_consecutive_losses': 2,
            'max_feed_age_seconds': 30.0,
        },
        'operator_start_request': {
            'session_id': 'smoke-session-1',
            'expected_mode': mode,
            'rounds': [
                {
                    'market_id': 'btc-5m',
                    'market_observed_at': '2026-04-14T20:00:00Z',
                    'feed_observed_at': '2026-04-14T20:00:00Z',
                    'health_state': 'ok',
                    'momentum': 0.0040,
                    'market_price': 99.6,
                    'reference_price': 100.0,
                    'yes_pressure': 80.0,
                    'no_pressure': 20.0,
                    'requested_notional_usd': 1.0,
                    'max_first_trade_notional_usd': 1.0,
                    'live_trading_enabled': False,
                }
            ],
        },
        'startup_prerequisites': _prereqs(tmp_path, include_live=include_live),
        'reconciliation_payload': _reconciliation_payload(reconciliation_status),
        'performance_config': {
            'min_sessions_for_review': 1,
            'min_trades_for_review': 0,
            'min_notional_for_review': 0.0,
            'repeated_mismatch_threshold': 2,
        },
        'allow_live_capable_smoke': False,
    }
    path = tmp_path / 'smoke.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


class StubSessionRunner:
    def __init__(self, session_event_log_path: Path, *, session_verdict: str = 'clean') -> None:
        self.session_event_log_path = session_event_log_path
        self.session_verdict = session_verdict
        self.calls: list[dict] = []

    def __call__(self, session_config: dict[str, object]) -> dict[str, object]:
        self.calls.append(session_config)
        if self.session_verdict == 'clean':
            events = [
                _session_event('session_started', session_id='smoke-session-1'),
                _session_event(
                    'trade_attempted',
                    session_id='smoke-session-1',
                    round_id='smoke-session-1:1',
                    attempted_notional=1.0,
                    execution_outcome='cancelled',
                    reconciliation_status='reconciled',
                    reasons=[],
                ),
                _session_event('session_stopped', session_id='smoke-session-1', stop_reason='rounds_exhausted'),
            ]
        else:
            events = [
                _session_event('session_started', session_id='smoke-session-1'),
                _session_event('trade_skipped', session_id='smoke-session-1', round_id='smoke-session-1:1', reasons=['stale_market:61.0s']),
                _session_event('trade_skipped', session_id='smoke-session-1', round_id='smoke-session-1:2', reasons=['missing_api_key']),
                _session_event('session_stopped', session_id='smoke-session-1', stop_reason='rounds_exhausted'),
            ]
        self.session_event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.session_event_log_path.open('w', encoding='utf-8') as handle:
            for event in events:
                handle.write(json.dumps({'event_type': event.event_type, 'timestamp': event.timestamp, 'details': event.details}, sort_keys=True) + '\n')
        return {
            'status': 'session_stopped',
            'stop_reason': 'rounds_exhausted',
            'trades_attempted': 1 if self.session_verdict == 'clean' else 0,
            'total_notional': 1.0 if self.session_verdict == 'clean' else 0.0,
            'consecutive_losses': 0,
            'event_count': len(events),
        }


class StubProbeResult:
    def __init__(self, status: str, reasons: tuple[str, ...]) -> None:
        self.status = status
        self.reasons = reasons


def test_clean_dry_run_smoke_returns_smoke_pass(tmp_path: Path):
    profile_path = _profile(tmp_path)
    session_log = Path(_prereqs(tmp_path)['session_event_log_path'])
    runner = StubSessionRunner(session_log, session_verdict='clean')

    result = run_end_to_end_smoke(profile_path, session_runner=runner)

    assert result.verdict == 'smoke_pass'
    assert result.reasons == ('operator_chain_validated',)
    assert len(runner.calls) == 1
    assert runner.calls[0]['requested_mode'] == 'dry_run'
    assert runner.calls[0]['live_trading_enabled'] is False


def test_degraded_but_safe_path_returns_smoke_caution(tmp_path: Path):
    profile_path = _profile(tmp_path, mode='capped_live')
    session_log = Path(_prereqs(tmp_path)['session_event_log_path'])
    runner = StubSessionRunner(session_log, session_verdict='clean')

    result = run_end_to_end_smoke(
        profile_path,
        session_runner=runner,
        probe_runner=lambda _path: StubProbeResult('ready_live', ('probe_ok',)),
    )

    assert result.verdict == 'smoke_caution'
    assert 'live_mode_stopped_at_probe' in result.reasons
    assert runner.calls == []


def test_broken_prerequisite_path_returns_smoke_fail(tmp_path: Path):
    profile_path = _profile(tmp_path)
    payload = json.loads(profile_path.read_text(encoding='utf-8'))
    payload['startup_prerequisites'] = {'runtime_launcher_script_path': 'missing'}
    profile_path.write_text(json.dumps(payload), encoding='utf-8')

    result = run_end_to_end_smoke(profile_path)

    assert result.verdict == 'smoke_fail'
    assert result.reasons == ('startup:invalid_startup_prerequisites',)


def test_smoke_path_never_places_uncontrolled_live_orders(tmp_path: Path):
    profile_path = _profile(tmp_path)
    session_log = Path(_prereqs(tmp_path)['session_event_log_path'])
    runner = StubSessionRunner(session_log, session_verdict='clean')
    original = profile_path.read_text(encoding='utf-8')

    result = run_end_to_end_smoke(profile_path, session_runner=runner)

    assert result.verdict == 'smoke_pass'
    assert profile_path.read_text(encoding='utf-8') == original
    assert len(runner.calls) == 1
    assert runner.calls[0]['requested_mode'] == 'dry_run'


def test_final_smoke_verdict_always_emitted(tmp_path: Path):
    profile_path = _profile(tmp_path)
    session_log = Path(_prereqs(tmp_path)['session_event_log_path'])
    runner = StubSessionRunner(session_log, session_verdict='clean')

    result = run_end_to_end_smoke(profile_path, session_runner=runner)

    assert result.events[-1].event_type == 'smoke_verdict_emitted'
