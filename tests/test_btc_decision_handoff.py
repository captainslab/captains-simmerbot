from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "skills" / "btc-sprint-stack" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from decision_handoff import (
    DecisionHandoffError,
    FeatureSnapshot,
    create_decision_record,
)


def _snapshot(**overrides) -> FeatureSnapshot:
    base = dict(
        round_id="r1",
        market_id="m1",
        ts_utc=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
        feed_status="ok",
        sufficient_data=True,
        stale=False,
        malformed=False,
        fully_scored=True,
        feature_summary={"momentum": 0.0},
    )
    base.update(overrides)
    return FeatureSnapshot(**base)


def test_decision_handoff_happy_path_records_decision():
    decision = create_decision_record(_snapshot())
    assert decision.final_action == "hold"
    assert decision.gate_result == "pass"
    assert decision.no_trade_basis is None


def test_decision_handoff_insufficient_data_records_no_trade():
    decision = create_decision_record(_snapshot(sufficient_data=False, fully_scored=False))
    assert decision.final_action == "no_trade"
    assert decision.no_trade_basis == "insufficient_data"


def test_decision_handoff_malformed_snapshot_is_typed_no_trade():
    decision = create_decision_record(_snapshot(malformed=True, sufficient_data=False, fully_scored=False))
    assert decision.final_action == "no_trade"
    assert decision.no_trade_basis == "malformed_feature_snapshot"


def test_decision_handoff_invalid_feed_status_raises_typed_failure():
    with pytest.raises(DecisionHandoffError, match="unsupported feed_status"):
        create_decision_record(_snapshot(feed_status="unknown"))
