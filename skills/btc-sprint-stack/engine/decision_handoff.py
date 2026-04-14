from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class DecisionHandoffError(ValueError):
    pass


@dataclass(frozen=True)
class FeatureSnapshot:
    round_id: str
    market_id: str
    ts_utc: datetime
    feed_status: str
    sufficient_data: bool
    stale: bool
    malformed: bool
    fully_scored: bool
    feature_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionRecord:
    round_id: str
    market_id: str
    feature_summary: dict[str, Any]
    vote_summary: str
    no_trade_basis: str | None
    edge_placeholder: float | None
    edge_unavailable_reason: str | None
    gate_result: str
    final_action: str
    ts_utc: datetime


def _validate_snapshot(snapshot: FeatureSnapshot) -> None:
    if not snapshot.round_id:
        raise DecisionHandoffError("round_id is required")
    if not snapshot.market_id:
        raise DecisionHandoffError("market_id is required")
    if snapshot.feed_status not in {"ok", "stale", "error"}:
        raise DecisionHandoffError(f"unsupported feed_status: {snapshot.feed_status}")


def create_decision_record(snapshot: FeatureSnapshot) -> DecisionRecord:
    _validate_snapshot(snapshot)

    # Explicit no-trade bases are strict and ordered by safety priority.
    if snapshot.malformed:
        return DecisionRecord(
            round_id=snapshot.round_id,
            market_id=snapshot.market_id,
            feature_summary=dict(snapshot.feature_summary),
            vote_summary="not_scored",
            no_trade_basis="malformed_feature_snapshot",
            edge_placeholder=None,
            edge_unavailable_reason="malformed",
            gate_result="reject",
            final_action="no_trade",
            ts_utc=snapshot.ts_utc,
        )

    if snapshot.stale or snapshot.feed_status == "stale":
        return DecisionRecord(
            round_id=snapshot.round_id,
            market_id=snapshot.market_id,
            feature_summary=dict(snapshot.feature_summary),
            vote_summary="not_scored",
            no_trade_basis="stale_data",
            edge_placeholder=None,
            edge_unavailable_reason="stale_data",
            gate_result="reject",
            final_action="no_trade",
            ts_utc=snapshot.ts_utc,
        )

    if not snapshot.sufficient_data:
        return DecisionRecord(
            round_id=snapshot.round_id,
            market_id=snapshot.market_id,
            feature_summary=dict(snapshot.feature_summary),
            vote_summary="not_scored",
            no_trade_basis="insufficient_data",
            edge_placeholder=None,
            edge_unavailable_reason="insufficient_data",
            gate_result="reject",
            final_action="no_trade",
            ts_utc=snapshot.ts_utc,
        )

    if not snapshot.fully_scored:
        return DecisionRecord(
            round_id=snapshot.round_id,
            market_id=snapshot.market_id,
            feature_summary=dict(snapshot.feature_summary),
            vote_summary="placeholder_vote",
            no_trade_basis="not_fully_scored",
            edge_placeholder=None,
            edge_unavailable_reason="not_fully_scored",
            gate_result="reject",
            final_action="no_trade",
            ts_utc=snapshot.ts_utc,
        )

    # Handoff-layer placeholder decision for integration (not strategy logic).
    return DecisionRecord(
        round_id=snapshot.round_id,
        market_id=snapshot.market_id,
        feature_summary=dict(snapshot.feature_summary),
        vote_summary="placeholder_vote",
        no_trade_basis=None,
        edge_placeholder=0.0,
        edge_unavailable_reason=None,
        gate_result="pass",
        final_action="hold",
        ts_utc=snapshot.ts_utc,
    )
