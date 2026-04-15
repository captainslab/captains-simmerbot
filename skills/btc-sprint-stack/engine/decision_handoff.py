from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from engine.feature_snapshot_builder import FeatureSnapshot
from engine.risk_gate import RiskGateResult, evaluate_risk_gate
from engine.vote_engine import DecisionOutcome, VoteEngine
from execution.trade_executor import DecisionRecord


def _build_feature_summary(snapshot: FeatureSnapshot) -> dict:
    return {
        'observed_at': snapshot.observed_at,
        'age_seconds': round(snapshot.age_seconds, 3),
        'available_balance_usdc': snapshot.available_balance_usdc,
        'health_state': snapshot.health_state,
        'signals': [
            {
                'name': signal.name,
                'family': signal.family,
                'direction': signal.direction,
                'strength': round(signal.strength, 4),
                'weight': round(signal.weight, 4),
                'raw_value': round(signal.raw_value, 6),
            }
            for signal in snapshot.signals
        ],
        'metadata': dict(snapshot.metadata),
    }


def _build_vote_summary(vote: DecisionOutcome) -> dict:
    return {
        'used_signals': list(vote.used_signals),
        'suppressed_signals': list(vote.suppressed_signals),
        'confidence': round(vote.confidence, 4),
        'reasoning': vote.reasoning,
        'signal_data': dict(vote.signal_data),
    }


def _build_gate_summary(gate: RiskGateResult) -> dict:
    return {
        'allowed': gate.allowed,
        'reasons': list(gate.reasons),
        'observed_edge': round(gate.observed_edge, 4),
    }


def build_decision_record(
    *,
    round_id: str,
    snapshot: FeatureSnapshot,
    vote: DecisionOutcome,
    gate: RiskGateResult,
    trade_amount_usd: float,
    now: datetime | None = None,
) -> DecisionRecord:
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    signal_data = dict(vote.signal_data)
    signal_data['gate_allowed'] = gate.allowed
    signal_data['gate_reason_count'] = len(gate.reasons)

    return DecisionRecord(
        decision_id=round_id,
        round_id=round_id,
        market_id=snapshot.market_id,
        action=vote.action,
        final_action=vote.action,
        amount=trade_amount_usd,
        timestamp=timestamp.isoformat(),
        feature_summary=_build_feature_summary(snapshot),
        vote_summary=_build_vote_summary(vote),
        edge=vote.edge,
        gate_result=_build_gate_summary(gate),
        reasoning=vote.reasoning,
        signal_data=signal_data,
    )


class DecisionHandoff:
    def __init__(self, *, vote_engine: VoteEngine) -> None:
        self._vote_engine = vote_engine

    def create_decision(
        self,
        *,
        round_id: str,
        snapshot: FeatureSnapshot,
        trade_amount_usd: float,
        now: datetime | None = None,
    ) -> DecisionRecord:
        vote = self._vote_engine.decide(snapshot)
        gate = evaluate_risk_gate(snapshot, edge=vote.edge, config=self._vote_engine.config.risk_gate)
        return build_decision_record(
            round_id=round_id,
            snapshot=snapshot,
            vote=vote,
            gate=gate,
            trade_amount_usd=trade_amount_usd,
            now=now,
        )
