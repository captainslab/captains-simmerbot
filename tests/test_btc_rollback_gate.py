from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from execution.rollback_gate import evaluate_rollback
from scripts.apply_rollback import run_apply_rollback


def _promotion_state(status: str = 'promoted') -> dict:
    return {
        'status': status,
        'profile': _current_profile(),
        'reasons': ['promotion_approved'] if status == 'promoted' else [],
    }


def _current_profile() -> dict:
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


def _prior_safe_profile() -> dict:
    return {
        'profile_name': 'capped-safe-v1',
        'max_trades_per_session': 2,
        'max_notional_per_session': 5.0,
        'max_consecutive_losses': 2,
        'max_feed_age_seconds': 30.0,
    }


def _performance_summary(*, unresolved: int = 0, mismatch: int = 0, blocked_sessions: int = 0, verdict: str = 'stay_capped') -> dict:
    return {
        'promotion_verdict': verdict,
        'sessions_counted': 3,
        'trades_counted': 4,
        'win_loss_summary': {'wins': 2, 'losses': 2},
        'notional_summary': {'total': 6.0, 'average_per_session': 2.0},
        'readiness_failure_count': 0,
        'reconciliation_mismatch_count': mismatch,
        'reconciliation_unresolved_count': unresolved,
        'blocked_session_count': blocked_sessions,
        'caution_session_count': 1,
        'promotion_reason_codes': ['caution_sessions_present'] if verdict == 'stay_capped' else [],
        'events': [
            {
                'event_type': 'performance_summary_built',
                'timestamp': '2026-04-14T00:00:00+00:00',
                'details': {},
            },
            {
                'event_type': 'promotion_verdict_emitted',
                'timestamp': '2026-04-14T00:00:01+00:00',
                'details': {'promotion_verdict': verdict},
            },
        ],
    }


def _trigger_config(*, operator_requested: bool = False, mismatch_threshold: int = 2, blocked_session_threshold: int = 2) -> dict:
    return {
        'operator_requested': operator_requested,
        'mismatch_threshold': mismatch_threshold,
        'blocked_session_threshold': blocked_session_threshold,
    }


def test_mismatch_heavy_path_triggers_rolled_back():
    decision = evaluate_rollback(
        promotion_state=_promotion_state(),
        current_profile=_current_profile(),
        prior_safe_profile=_prior_safe_profile(),
        summary=_performance_summary(mismatch=2),
        trigger_config=_trigger_config(),
    )

    assert decision.status == 'rolled_back'
    assert decision.profile == _prior_safe_profile()
    assert 'repeated_reconciliation_mismatch' in decision.reasons


def test_explicit_operator_rollback_triggers_rolled_back():
    decision = evaluate_rollback(
        promotion_state=_promotion_state(),
        current_profile=_current_profile(),
        prior_safe_profile=_prior_safe_profile(),
        summary=_performance_summary(),
        trigger_config=_trigger_config(operator_requested=True),
    )

    assert decision.status == 'rolled_back'
    assert decision.profile == _prior_safe_profile()
    assert 'operator_rollback_requested' in decision.reasons


def test_invalid_missing_prior_safe_profile_returns_blocked():
    missing = evaluate_rollback(
        promotion_state=_promotion_state(),
        current_profile=_current_profile(),
        prior_safe_profile=None,
        summary=_performance_summary(mismatch=2),
        trigger_config=_trigger_config(),
    )
    invalid = evaluate_rollback(
        promotion_state=_promotion_state(),
        current_profile=_current_profile(),
        prior_safe_profile={'profile_name': 'broken'},
        summary=_performance_summary(mismatch=2),
        trigger_config=_trigger_config(),
    )

    assert missing.status == 'blocked'
    assert missing.reasons == ('invalid_prior_safe_profile',)
    assert invalid.status == 'blocked'
    assert invalid.reasons == ('invalid_prior_safe_profile',)


def test_keep_current_path_preserves_promoted_profile():
    decision = evaluate_rollback(
        promotion_state=_promotion_state(),
        current_profile=_current_profile(),
        prior_safe_profile=_prior_safe_profile(),
        summary=_performance_summary(mismatch=1, blocked_sessions=1),
        trigger_config=_trigger_config(),
    )

    assert decision.status == 'keep_current'
    assert decision.profile == _current_profile()
    assert decision.reasons == ('rollback_not_triggered',)


def test_rollback_path_never_places_any_order(tmp_path: Path):
    promotion_state_path = tmp_path / 'promotion-state.json'
    current_profile_path = tmp_path / 'current-profile.json'
    prior_safe_profile_path = tmp_path / 'prior-safe-profile.json'
    performance_summary_path = tmp_path / 'performance-summary.json'
    trigger_config_path = tmp_path / 'trigger.json'
    output_path = tmp_path / 'rolled-back.json'
    event_log = tmp_path / 'rollback.jsonl'

    promotion_original = json.dumps(_promotion_state(), sort_keys=True)
    current_original = json.dumps(_current_profile(), sort_keys=True)
    prior_original = json.dumps(_prior_safe_profile(), sort_keys=True)
    summary_original = json.dumps(_performance_summary(mismatch=2), sort_keys=True)
    trigger_original = json.dumps(_trigger_config(), sort_keys=True)
    promotion_state_path.write_text(promotion_original, encoding='utf-8')
    current_profile_path.write_text(current_original, encoding='utf-8')
    prior_safe_profile_path.write_text(prior_original, encoding='utf-8')
    performance_summary_path.write_text(summary_original, encoding='utf-8')
    trigger_config_path.write_text(trigger_original, encoding='utf-8')

    result = run_apply_rollback(
        promotion_state_path=promotion_state_path,
        current_profile_path=current_profile_path,
        prior_safe_profile_path=prior_safe_profile_path,
        performance_summary_path=performance_summary_path,
        trigger_config_path=trigger_config_path,
        output_profile_path=output_path,
        event_log_path=event_log,
    )

    assert result['status'] == 'rolled_back'
    assert promotion_state_path.read_text(encoding='utf-8') == promotion_original
    assert current_profile_path.read_text(encoding='utf-8') == current_original
    assert prior_safe_profile_path.read_text(encoding='utf-8') == prior_original
    assert performance_summary_path.read_text(encoding='utf-8') == summary_original
    assert trigger_config_path.read_text(encoding='utf-8') == trigger_original
    assert output_path.exists()
    assert len(event_log.read_text(encoding='utf-8').splitlines()) == len(result['events'])


def test_final_rollback_verdict_always_emitted():
    decision = evaluate_rollback(
        promotion_state=_promotion_state(status='remain_capped'),
        current_profile=_prior_safe_profile(),
        prior_safe_profile=_prior_safe_profile(),
        summary=_performance_summary(),
        trigger_config=_trigger_config(),
    )

    assert decision.events[-1].event_type == 'rollback_verdict_emitted'
    assert decision.status == 'keep_current'
