from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from runtime.mode_launcher import launch_active_mode
from scripts.run_active_mode import run_active_mode_from_files


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


def _rounds() -> list[dict]:
    return [
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
            'live_trading_enabled': True,
        }
    ]


def _request(mode: str) -> dict:
    return {
        'session_id': 'runtime-session-1',
        'expected_mode': mode,
        'rounds': _rounds(),
    }


def _deployment_mode(mode: str, profile: dict | None = None) -> dict:
    return {
        'mode': mode,
        'profile': profile,
        'reasons': [f'{mode}_enabled'],
    }


class StubRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, session_config: dict) -> dict:
        self.calls.append(session_config)
        return {
            'status': 'session_stopped',
            'stop_reason': 'rounds_exhausted',
            'trades_attempted': 0,
            'total_notional': 0.0,
            'consecutive_losses': 0,
            'event_count': 4,
        }


def test_disabled_mode_refuses_launch_cleanly():
    runner = StubRunner()

    result = launch_active_mode(
        deployment_mode=_deployment_mode('disabled'),
        active_profile=None,
        operator_start_request=_request('disabled'),
        session_runner=runner,
    )

    assert result.status == 'disabled'
    assert result.route == 'disabled'
    assert result.reasons == ('deployment_disabled',)
    assert runner.calls == []


def test_dry_run_mode_launches_only_dry_run_path():
    runner = StubRunner()

    result = launch_active_mode(
        deployment_mode=_deployment_mode('dry_run', _capped_profile()),
        active_profile=_capped_profile(),
        operator_start_request=_request('dry_run'),
        session_runner=runner,
    )

    assert result.status == 'dry_run'
    assert result.route == 'dry_run_session'
    assert len(runner.calls) == 1
    assert runner.calls[0]['requested_mode'] == 'dry_run'
    assert runner.calls[0]['live_trading_enabled'] is False


def test_capped_live_launches_only_with_valid_capped_safe_profile():
    runner = StubRunner()
    result = launch_active_mode(
        deployment_mode=_deployment_mode('capped_live', _capped_profile()),
        active_profile=_capped_profile(),
        operator_start_request=_request('capped_live'),
        session_runner=runner,
    )
    blocked = launch_active_mode(
        deployment_mode=_deployment_mode('capped_live', _capped_profile()),
        active_profile=_promoted_profile(),
        operator_start_request=_request('capped_live'),
        session_runner=runner,
    )

    assert result.status == 'capped_live'
    assert result.route == 'session_controller'
    assert runner.calls[0]['requested_mode'] == 'live'
    assert runner.calls[0]['live_trading_enabled'] is True
    assert blocked.status == 'blocked'
    assert blocked.reasons == ('conflicting_profile_state',)


def test_promoted_live_launches_only_with_valid_promoted_state():
    runner = StubRunner()
    result = launch_active_mode(
        deployment_mode=_deployment_mode('promoted_live', _promoted_profile()),
        active_profile=_promoted_profile(),
        operator_start_request=_request('promoted_live'),
        session_runner=runner,
    )
    blocked = launch_active_mode(
        deployment_mode=_deployment_mode('promoted_live', _promoted_profile()),
        active_profile=_capped_profile(),
        operator_start_request=_request('promoted_live'),
        session_runner=runner,
    )

    assert result.status == 'promoted_live'
    assert result.route == 'session_controller'
    assert blocked.status == 'blocked'
    assert blocked.reasons == ('conflicting_profile_state',)


def test_rolled_back_launches_only_with_valid_rolled_back_profile():
    runner = StubRunner()
    result = launch_active_mode(
        deployment_mode=_deployment_mode('rolled_back', _capped_profile()),
        active_profile=_capped_profile(),
        operator_start_request=_request('rolled_back'),
        session_runner=runner,
    )
    blocked = launch_active_mode(
        deployment_mode=_deployment_mode('rolled_back', _capped_profile()),
        active_profile=_promoted_profile(),
        operator_start_request=_request('rolled_back'),
        session_runner=runner,
    )

    assert result.status == 'rolled_back'
    assert result.route == 'session_controller'
    assert blocked.status == 'blocked'
    assert blocked.reasons == ('conflicting_profile_state',)


def test_invalid_conflicting_state_returns_blocked():
    runner = StubRunner()
    result = launch_active_mode(
        deployment_mode=_deployment_mode('blocked', _capped_profile()),
        active_profile=_capped_profile(),
        operator_start_request=_request('capped_live'),
        session_runner=runner,
    )

    assert result.status == 'blocked'
    assert result.reasons == ('invalid_runtime_mode:blocked',)
    assert runner.calls == []


def test_launcher_path_never_bypasses_existing_controls(tmp_path: Path):
    deployment_mode_path = tmp_path / 'deployment-mode.json'
    active_profile_path = tmp_path / 'active-profile.json'
    operator_request_path = tmp_path / 'start-request.json'
    runtime_event_log = tmp_path / 'runtime.jsonl'
    output_state = tmp_path / 'runtime-state.json'
    deployment_original = json.dumps(_deployment_mode('capped_live', _capped_profile()), sort_keys=True)
    profile_original = json.dumps(_capped_profile(), sort_keys=True)
    request_original = json.dumps(_request('capped_live'), sort_keys=True)
    deployment_mode_path.write_text(deployment_original, encoding='utf-8')
    active_profile_path.write_text(profile_original, encoding='utf-8')
    operator_request_path.write_text(request_original, encoding='utf-8')

    result = run_active_mode_from_files(
        deployment_mode_path=deployment_mode_path,
        active_profile_path=active_profile_path,
        operator_start_request_path=operator_request_path,
        runtime_event_log_path=runtime_event_log,
        output_state_path=output_state,
    )

    assert result['status'] == 'blocked' or result['status'] == 'capped_live'
    assert deployment_mode_path.read_text(encoding='utf-8') == deployment_original
    assert active_profile_path.read_text(encoding='utf-8') == profile_original
    assert operator_request_path.read_text(encoding='utf-8') == request_original
    assert output_state.exists()
    assert len(runtime_event_log.read_text(encoding='utf-8').splitlines()) == len(result['events'])


def test_final_runtime_launch_verdict_always_emitted():
    result = launch_active_mode(
        deployment_mode=_deployment_mode('disabled'),
        active_profile=None,
        operator_start_request=_request('disabled'),
        session_runner=StubRunner(),
    )

    assert result.events[-1].event_type == 'runtime_launch_verdict_emitted'
