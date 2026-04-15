from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from runtime.startup_check import run_startup_check
from scripts.run_startup_check import run_startup_check_from_files


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


def _deployment_mode(mode: str, profile: dict | None = None) -> dict:
    return {
        'mode': mode,
        'profile': profile,
        'reasons': [f'{mode}_enabled'],
    }


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


def test_valid_dry_run_startup_returns_startup_ready(tmp_path: Path):
    result = run_startup_check(
        deployment_mode=_deployment_mode('dry_run'),
        active_profile=_capped_profile(),
        prerequisites=_prereqs(tmp_path),
    )

    assert result.status == 'startup_ready'
    assert result.mode == 'dry_run'
    assert result.reasons == ('dry_run_path_ready',)


def test_valid_live_startup_returns_startup_ready_only_when_prereqs_present(tmp_path: Path):
    capped = run_startup_check(
        deployment_mode=_deployment_mode('capped_live', _capped_profile()),
        active_profile=_capped_profile(),
        prerequisites=_prereqs(tmp_path / 'capped'),
    )
    promoted = run_startup_check(
        deployment_mode=_deployment_mode('promoted_live', _promoted_profile()),
        active_profile=_promoted_profile(),
        prerequisites=_prereqs(tmp_path / 'promoted'),
    )

    assert capped.status == 'startup_ready'
    assert capped.mode == 'capped_live'
    assert promoted.status == 'startup_ready'
    assert promoted.mode == 'promoted_live'


def test_missing_profile_or_config_returns_startup_blocked(tmp_path: Path):
    missing_profile = run_startup_check(
        deployment_mode=_deployment_mode('dry_run'),
        active_profile=None,
        prerequisites=_prereqs(tmp_path / 'profile'),
    )
    invalid_config = run_startup_check(
        deployment_mode=_deployment_mode('dry_run'),
        active_profile=_capped_profile(),
        prerequisites={'runtime_launcher_script_path': 'missing'},
    )

    assert missing_profile.status == 'startup_blocked'
    assert missing_profile.reasons == ('missing_required_profile',)
    assert invalid_config.status == 'startup_blocked'
    assert invalid_config.reasons == ('invalid_startup_prerequisites',)


def test_live_capable_mode_with_failed_prereqs_returns_dry_run_only_or_blocked(tmp_path: Path):
    dry_run_only = run_startup_check(
        deployment_mode=_deployment_mode('capped_live', _capped_profile()),
        active_profile=_capped_profile(),
        prerequisites=_prereqs(tmp_path / 'degraded', include_live=False),
    )
    blocked = run_startup_check(
        deployment_mode=_deployment_mode('promoted_live', _promoted_profile()),
        active_profile=_capped_profile(),
        prerequisites=_prereqs(tmp_path / 'blocked'),
    )

    assert dry_run_only.status == 'startup_dry_run_only'
    assert 'missing_readiness_probe_script' in dry_run_only.reasons
    assert blocked.status == 'startup_blocked'
    assert blocked.reasons == ('invalid_promoted_profile',)


def test_startup_check_never_places_any_order(tmp_path: Path):
    deployment_mode_path = tmp_path / 'deployment-mode.json'
    active_profile_path = tmp_path / 'active-profile.json'
    prerequisites_path = tmp_path / 'prereqs.json'
    output_state = tmp_path / 'startup-state.json'
    event_log = tmp_path / 'startup.jsonl'
    deployment_original = json.dumps(_deployment_mode('capped_live', _capped_profile()), sort_keys=True)
    profile_original = json.dumps(_capped_profile(), sort_keys=True)
    prereqs_original = json.dumps(_prereqs(tmp_path / 'files'), sort_keys=True)
    deployment_mode_path.write_text(deployment_original, encoding='utf-8')
    active_profile_path.write_text(profile_original, encoding='utf-8')
    prerequisites_path.write_text(prereqs_original, encoding='utf-8')

    result = run_startup_check_from_files(
        deployment_mode_path=deployment_mode_path,
        active_profile_path=active_profile_path,
        prerequisites_path=prerequisites_path,
        output_state_path=output_state,
        event_log_path=event_log,
    )

    assert result['status'] == 'startup_ready'
    assert deployment_mode_path.read_text(encoding='utf-8') == deployment_original
    assert active_profile_path.read_text(encoding='utf-8') == profile_original
    assert prerequisites_path.read_text(encoding='utf-8') == prereqs_original
    assert output_state.exists()
    assert len(event_log.read_text(encoding='utf-8').splitlines()) == len(result['events'])


def test_final_startup_verdict_always_emitted(tmp_path: Path):
    result = run_startup_check(
        deployment_mode=_deployment_mode('disabled'),
        active_profile=None,
        prerequisites=_prereqs(tmp_path),
    )

    assert result.events[-1].event_type == 'startup_verdict_emitted'
    assert result.status == 'startup_ready'
