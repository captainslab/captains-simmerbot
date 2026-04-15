from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.finalize_operator_path import finalize_operator_path, run_finalize_operator_path


def _operator_state(*, startup_status: str = 'startup_ready', smoke_verdict: str = 'smoke_pass') -> dict:
    return {
        'startup_status': startup_status,
        'smoke_verdict': smoke_verdict,
        'reasons': [],
    }


def _capped_profile() -> dict:
    return {
        'profile_name': 'capped-safe-v1',
        'max_trades_per_session': 2,
        'max_notional_per_session': 5.0,
        'max_consecutive_losses': 2,
        'max_feed_age_seconds': 30.0,
    }


def _promoted_profile() -> dict:
    return {
        'profile_name': 'reviewed-scale-up-v1',
        'approval_id': 'review-1',
        'approved_by': 'operator',
        'approved_at': '2026-04-14T20:00:00Z',
        'max_trades_per_session': 4,
        'max_notional_per_session': 8.0,
        'max_consecutive_losses': 2,
        'max_feed_age_seconds': 30.0,
    }


def _entrypoints() -> dict:
    return {
        'startup': 'python3 skills/btc-sprint-stack/scripts/run_startup_check.py --deployment-mode path/to/deployment-mode.json --active-profile path/to/active-profile.json --prerequisites path/to/startup-prereqs.json',
        'smoke': 'python3 skills/btc-sprint-stack/scripts/run_end_to_end_smoke.py --config skills/btc-sprint-stack/config/smoke_profile.example.json --event-log path/to/smoke-events.jsonl',
        'probe': 'python3 skills/btc-sprint-stack/scripts/prove_live_ready.py --config skills/btc-sprint-stack/config/live_probe.example.json --event-log path/to/probe-events.jsonl',
        'session_run': 'python3 skills/btc-sprint-stack/scripts/run_active_mode.py --deployment-mode path/to/deployment-mode.json --active-profile path/to/active-profile.json --operator-start-request path/to/start-request.json --session-event-log path/to/session-events.jsonl --runtime-event-log path/to/runtime-events.jsonl',
        'reconcile_report': 'python3 skills/btc-sprint-stack/scripts/reconcile_last_trade.py --payload path/to/reconciliation-payload.json --event-log path/to/reconciliation-events.jsonl && python3 skills/btc-sprint-stack/scripts/report_last_session.py --event-log path/to/session-events.jsonl',
        'rollback': 'python3 skills/btc-sprint-stack/scripts/apply_rollback.py --promotion-state path/to/promotion-state.json --current-profile path/to/current-profile.json --prior-safe-profile path/to/capped-safe-profile.json --performance-summary path/to/performance-summary.json --trigger-config path/to/rollback-trigger.json --output-profile path/to/rolled-back-profile.json --event-log path/to/rollback-events.jsonl',
    }


def _active_mode(mode: str = 'dry_run') -> dict:
    return {
        'approved_mode': mode,
        'script_entrypoints': _entrypoints(),
    }


def test_valid_active_path_finalizes_cleanly():
    result = finalize_operator_path(
        operator_state=_operator_state(),
        approved_mode='dry_run',
        active_profile=_capped_profile(),
        script_entrypoints=_entrypoints(),
    )

    assert result.status == 'finalized'
    assert result.active_mode == 'dry_run'
    assert result.active_profile == _capped_profile()


def test_conflicting_mode_profile_state_is_blocked():
    result = finalize_operator_path(
        operator_state=_operator_state(),
        approved_mode='promoted_live',
        active_profile=_capped_profile(),
        script_entrypoints=_entrypoints(),
    )

    assert result.status == 'blocked'
    assert result.reasons == ('conflicting_profile_state',)


def test_duplicate_deprecated_path_detection_is_explicit():
    duplicate = _entrypoints()
    duplicate['probe'] = duplicate['startup']
    duplicate_result = finalize_operator_path(
        operator_state=_operator_state(),
        approved_mode='dry_run',
        active_profile=_capped_profile(),
        script_entrypoints=duplicate,
    )
    deprecated = _entrypoints()
    deprecated['session_run'] = 'python3 skills/btc-sprint-stack/scripts/run_live_session.py --config path/to/session.json'
    deprecated_result = finalize_operator_path(
        operator_state=_operator_state(),
        approved_mode='dry_run',
        active_profile=_capped_profile(),
        script_entrypoints=deprecated,
    )

    assert duplicate_result.status == 'blocked'
    assert any(reason.startswith('duplicate_entrypoint:') for reason in duplicate_result.reasons)
    assert deprecated_result.status == 'blocked'
    assert 'deprecated_entrypoint:session_run:skills/btc-sprint-stack/scripts/run_live_session.py' in deprecated_result.reasons


def test_finalize_path_never_places_any_order(tmp_path: Path):
    operator_state_path = tmp_path / 'operator-state.json'
    active_mode_path = tmp_path / 'active-mode.json'
    active_profile_path = tmp_path / 'active-profile.json'
    output_state = tmp_path / 'active-path.json'
    event_log = tmp_path / 'finalize.jsonl'
    operator_original = json.dumps(_operator_state(), sort_keys=True)
    active_mode_original = json.dumps(_active_mode(), sort_keys=True)
    active_profile_original = json.dumps(_capped_profile(), sort_keys=True)
    operator_state_path.write_text(operator_original, encoding='utf-8')
    active_mode_path.write_text(active_mode_original, encoding='utf-8')
    active_profile_path.write_text(active_profile_original, encoding='utf-8')

    result = run_finalize_operator_path(
        operator_state_path=operator_state_path,
        active_mode_path=active_mode_path,
        active_profile_path=active_profile_path,
        output_state_path=output_state,
        event_log_path=event_log,
    )

    assert result['status'] == 'finalized'
    assert operator_state_path.read_text(encoding='utf-8') == operator_original
    assert active_mode_path.read_text(encoding='utf-8') == active_mode_original
    assert active_profile_path.read_text(encoding='utf-8') == active_profile_original
    assert output_state.exists()
    assert len(event_log.read_text(encoding='utf-8').splitlines()) == len(result['events'])


def test_final_active_path_verdict_always_emitted():
    result = finalize_operator_path(
        operator_state=_operator_state(),
        approved_mode='disabled',
        active_profile=None,
        script_entrypoints=_entrypoints(),
    )

    assert result.events[-1].event_type == 'active_path_verdict_emitted'
