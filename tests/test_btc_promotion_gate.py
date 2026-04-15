from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from execution.promotion_gate import evaluate_promotion_review
from reporting.performance_summary import build_performance_summary
from reporting.session_report import build_session_report
from execution.session_controller import SessionEvent
from scripts.apply_promotion_review import run_apply_promotion_review


def _session_event(event_type: str, **details) -> SessionEvent:
    from datetime import datetime, timezone

    return SessionEvent(
        event_type=event_type,
        timestamp=datetime.now(timezone.utc).isoformat(),
        details=details,
    )


def _promotion_summary(verdict: str = 'eligible_for_review', *, unresolved: int = 0, mismatch: int = 0, reasons: tuple[str, ...] = ()) -> dict:
    return {
        'promotion_verdict': verdict,
        'sessions_counted': 3,
        'trades_counted': 4,
        'win_loss_summary': {'wins': 4, 'losses': 0},
        'notional_summary': {'total': 4.5, 'average_per_session': 1.5},
        'readiness_failure_count': 0,
        'reconciliation_mismatch_count': mismatch,
        'reconciliation_unresolved_count': unresolved,
        'blocked_session_count': 0,
        'caution_session_count': 0,
        'promotion_reason_codes': list(reasons),
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


def _current_caps() -> dict:
    return {
        'max_trades_per_session': 2,
        'max_notional_per_session': 5.0,
        'max_consecutive_losses': 2,
        'max_feed_age_seconds': 30.0,
    }


def _approval() -> dict:
    return {
        'approval_id': 'review-1',
        'approved': True,
        'approved_by': 'operator',
        'approved_at': '2026-04-14T20:00:00Z',
        'profile_name': 'reviewed-scale-up-v1',
        'max_trades_per_session': 4,
        'max_notional_per_session': 8.0,
    }


def test_eligible_for_review_with_valid_approval_promotes():
    decision = evaluate_promotion_review(
        summary=_promotion_summary(),
        current_caps=_current_caps(),
        approval=_approval(),
    )

    assert decision.status == 'promoted'
    assert decision.profile['max_trades_per_session'] == 4
    assert decision.profile['max_notional_per_session'] == 8.0


def test_eligible_for_review_without_approval_blocks_with_explicit_reason():
    decision = evaluate_promotion_review(
        summary=_promotion_summary(),
        current_caps=_current_caps(),
        approval=None,
    )

    assert decision.status == 'blocked'
    assert decision.reasons == ('missing_promotion_approval',)


def test_blocked_verdict_cannot_promote():
    decision = evaluate_promotion_review(
        summary=_promotion_summary(verdict='blocked', reasons=('unresolved_reconciliation_present',)),
        current_caps=_current_caps(),
        approval=_approval(),
    )

    assert decision.status == 'blocked'
    assert decision.profile == _current_caps()


def test_promoted_profile_still_preserves_existing_session_stop_controls():
    decision = evaluate_promotion_review(
        summary=_promotion_summary(),
        current_caps=_current_caps(),
        approval=_approval(),
    )

    assert decision.status == 'promoted'
    assert decision.profile['max_consecutive_losses'] == 2
    assert decision.profile['max_feed_age_seconds'] == 30.0


def test_promotion_path_never_places_any_order(tmp_path: Path):
    summary_path = tmp_path / 'summary.json'
    caps_path = tmp_path / 'caps.json'
    approval_path = tmp_path / 'approval.json'
    output_path = tmp_path / 'promoted.json'
    event_log = tmp_path / 'promotion.jsonl'
    summary_payload = _promotion_summary()
    caps_payload = _current_caps()
    approval_payload = _approval()
    summary_original = json.dumps(summary_payload, sort_keys=True)
    caps_original = json.dumps(caps_payload, sort_keys=True)
    approval_original = json.dumps(approval_payload, sort_keys=True)
    summary_path.write_text(summary_original, encoding='utf-8')
    caps_path.write_text(caps_original, encoding='utf-8')
    approval_path.write_text(approval_original, encoding='utf-8')

    result = run_apply_promotion_review(
        promotion_summary_path=summary_path,
        current_caps_path=caps_path,
        approval_path=approval_path,
        output_profile_path=output_path,
        event_log_path=event_log,
    )

    assert result['status'] == 'promoted'
    assert summary_path.read_text(encoding='utf-8') == summary_original
    assert caps_path.read_text(encoding='utf-8') == caps_original
    assert approval_path.read_text(encoding='utf-8') == approval_original
    assert output_path.exists()
    assert len(event_log.read_text(encoding='utf-8').splitlines()) == len(result['events'])


def test_final_promotion_verdict_is_always_emitted():
    decision = evaluate_promotion_review(
        summary=_promotion_summary(verdict='stay_capped', reasons=('insufficient_sessions',)),
        current_caps=_current_caps(),
        approval=None,
    )

    assert decision.events[-1].event_type == 'promotion_verdict_emitted'
    assert decision.status == 'remain_capped'
