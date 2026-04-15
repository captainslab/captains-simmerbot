from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from execution.deployment_mode import evaluate_deployment_mode
from scripts.set_deployment_mode import run_set_deployment_mode


def _capped_profile() -> dict:
    return {
        'profile_name': 'capped-safe-v1',
        'max_trades_per_session': 2,
        'max_notional_per_session': 5.0,
        'max_consecutive_losses': 2,
        'max_feed_age_seconds': 30.0,
    }


def _promoted_state() -> dict:
    return {
        'status': 'promoted',
        'profile': {
            'profile_name': 'reviewed-scale-up-v1',
            'approval_id': 'review-1',
            'approved_by': 'operator',
            'approved_at': '2026-04-14T20:00:00Z',
            'max_trades_per_session': 4,
            'max_notional_per_session': 8.0,
            'max_consecutive_losses': 2,
            'max_feed_age_seconds': 30.0,
        },
        'reasons': ['promotion_approved'],
    }


def _rollback_state() -> dict:
    return {
        'status': 'rolled_back',
        'profile': _capped_profile(),
        'reasons': ['repeated_reconciliation_mismatch'],
    }


def _keep_current_rollback_state() -> dict:
    return {
        'status': 'keep_current',
        'profile': _promoted_state()['profile'],
        'reasons': ['rollback_not_triggered'],
    }


def _request(current_mode: str, requested_mode: str) -> dict:
    return {
        'current_mode': current_mode,
        'requested_mode': requested_mode,
    }


def test_disabled_to_dry_run_works():
    decision = evaluate_deployment_mode(
        profile_state=None,
        promotion_state=None,
        rollback_state=None,
        operator_request=_request('disabled', 'dry_run'),
    )

    assert decision.mode == 'dry_run'
    assert decision.profile is None


def test_dry_run_to_capped_live_works_only_with_valid_capped_safe_profile():
    decision = evaluate_deployment_mode(
        profile_state=_capped_profile(),
        promotion_state=None,
        rollback_state=None,
        operator_request=_request('dry_run', 'capped_live'),
    )
    blocked = evaluate_deployment_mode(
        profile_state=_promoted_state()['profile'],
        promotion_state=None,
        rollback_state=None,
        operator_request=_request('dry_run', 'capped_live'),
    )

    assert decision.mode == 'capped_live'
    assert decision.profile == _capped_profile()
    assert blocked.mode == 'blocked'
    assert blocked.reasons == ('invalid_capped_safe_profile',)


def test_capped_live_to_promoted_live_works_only_with_approved_promotion_state():
    decision = evaluate_deployment_mode(
        profile_state=_capped_profile(),
        promotion_state=_promoted_state(),
        rollback_state=None,
        operator_request=_request('capped_live', 'promoted_live'),
    )
    blocked = evaluate_deployment_mode(
        profile_state=_capped_profile(),
        promotion_state={'status': 'remain_capped', 'profile': _capped_profile(), 'reasons': ['insufficient_sessions']},
        rollback_state=None,
        operator_request=_request('capped_live', 'promoted_live'),
    )

    assert decision.mode == 'promoted_live'
    assert decision.profile == _promoted_state()['profile']
    assert blocked.mode == 'blocked'
    assert blocked.reasons == ('invalid_promoted_profile_state',)


def test_promoted_live_to_rolled_back_works_only_through_valid_rollback_outcome():
    decision = evaluate_deployment_mode(
        profile_state=_promoted_state()['profile'],
        promotion_state=_promoted_state(),
        rollback_state=_rollback_state(),
        operator_request=_request('promoted_live', 'rolled_back'),
    )
    blocked = evaluate_deployment_mode(
        profile_state=_promoted_state()['profile'],
        promotion_state=_promoted_state(),
        rollback_state={'status': 'keep_current', 'profile': _promoted_state()['profile'], 'reasons': ['rollback_not_triggered']},
        operator_request=_request('promoted_live', 'rolled_back'),
    )

    assert decision.mode == 'rolled_back'
    assert decision.profile == _capped_profile()
    assert blocked.mode == 'blocked'
    assert blocked.reasons == ('invalid_rollback_state',)


def test_invalid_transitions_return_blocked():
    decision = evaluate_deployment_mode(
        profile_state=_capped_profile(),
        promotion_state=None,
        rollback_state=None,
        operator_request=_request('disabled', 'promoted_live'),
    )

    assert decision.mode == 'blocked'
    assert decision.reasons == ('invalid_mode_transition:disabled->promoted_live',)


def test_mode_change_path_never_places_any_order(tmp_path: Path):
    request_path = tmp_path / 'request.json'
    profile_path = tmp_path / 'profile.json'
    promotion_path = tmp_path / 'promotion.json'
    rollback_path = tmp_path / 'rollback.json'
    output_path = tmp_path / 'deployment-state.json'
    event_log = tmp_path / 'deployment.jsonl'
    request_original = json.dumps(_request('capped_live', 'promoted_live'), sort_keys=True)
    profile_original = json.dumps(_capped_profile(), sort_keys=True)
    promotion_original = json.dumps(_promoted_state(), sort_keys=True)
    rollback_original = json.dumps(_keep_current_rollback_state(), sort_keys=True)
    request_path.write_text(request_original, encoding='utf-8')
    profile_path.write_text(profile_original, encoding='utf-8')
    promotion_path.write_text(promotion_original, encoding='utf-8')
    rollback_path.write_text(rollback_original, encoding='utf-8')

    result = run_set_deployment_mode(
        operator_request_path=request_path,
        profile_state_path=profile_path,
        promotion_state_path=promotion_path,
        rollback_state_path=rollback_path,
        output_state_path=output_path,
        event_log_path=event_log,
    )

    assert result['mode'] == 'promoted_live'
    assert request_path.read_text(encoding='utf-8') == request_original
    assert profile_path.read_text(encoding='utf-8') == profile_original
    assert promotion_path.read_text(encoding='utf-8') == promotion_original
    assert rollback_path.read_text(encoding='utf-8') == rollback_original
    assert output_path.exists()
    assert len(event_log.read_text(encoding='utf-8').splitlines()) == len(result['events'])


def test_final_deployment_mode_verdict_always_emitted():
    decision = evaluate_deployment_mode(
        profile_state=None,
        promotion_state=None,
        rollback_state=None,
        operator_request=_request('disabled', 'dry_run'),
    )

    assert decision.events[-1].event_type == 'deployment_mode_verdict_emitted'
    assert decision.mode == 'dry_run'
