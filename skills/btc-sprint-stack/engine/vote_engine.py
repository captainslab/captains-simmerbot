from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engine.feature_snapshot_builder import FeatureSignal, FeatureSnapshot
from engine.risk_gate import RiskGateConfig, evaluate_risk_gate


@dataclass(frozen=True)
class VoteEngineConfig:
    signal_source: str = 'vote_engine_v1'
    risk_gate: RiskGateConfig = field(default_factory=RiskGateConfig)


@dataclass(frozen=True)
class DecisionOutcome:
    action: str
    edge: float
    confidence: float
    reasoning: str
    signal_data: dict[str, Any]
    blocked_reasons: tuple[str, ...]
    used_signals: tuple[str, ...]
    suppressed_signals: tuple[str, ...]


class VoteEngine:
    def __init__(self, *, config: VoteEngineConfig | None = None) -> None:
        self._config = config or VoteEngineConfig()

    @property
    def config(self) -> VoteEngineConfig:
        return self._config

    def decide(self, snapshot: FeatureSnapshot) -> DecisionOutcome:
        used_signals, suppressed_signals = self._deduplicate(snapshot.signals)

        signed_edge = 0.0
        yes_signals = 0
        no_signals = 0
        for signal in used_signals:
            contribution = signal.weight * signal.strength
            if signal.direction == 'yes':
                signed_edge += contribution
                yes_signals += 1
            elif signal.direction == 'no':
                signed_edge -= contribution
                no_signals += 1

        edge = abs(signed_edge)
        confidence = min(0.95, 0.50 + (edge * 0.35) + (0.05 * min(len(used_signals), 2)))
        gate = evaluate_risk_gate(snapshot, edge=edge, config=self._config.risk_gate)

        if not gate.allowed:
            action = 'no_trade'
        elif signed_edge > 0:
            action = 'buy_yes'
        elif signed_edge < 0:
            action = 'buy_no'
        else:
            action = 'no_trade'

        reasoning = self._build_reasoning(
            action=action,
            used_signals=used_signals,
            suppressed_signals=suppressed_signals,
            blocked_reasons=gate.reasons,
        )
        signal_data = {
            'edge': round(edge, 4),
            'confidence': round(confidence, 4),
            'signal_source': self._config.signal_source,
            'used_signal_count': len(used_signals),
            'suppressed_signal_count': len(suppressed_signals),
            'yes_signal_count': yes_signals,
            'no_signal_count': no_signals,
            'snapshot_age_seconds': round(snapshot.age_seconds, 3),
        }
        return DecisionOutcome(
            action=action,
            edge=edge,
            confidence=confidence,
            reasoning=reasoning,
            signal_data=signal_data,
            blocked_reasons=gate.reasons,
            used_signals=tuple(signal.name for signal in used_signals),
            suppressed_signals=tuple(signal.name for signal in suppressed_signals),
        )

    @staticmethod
    def _deduplicate(signals: tuple[FeatureSignal, ...]) -> tuple[list[FeatureSignal], list[FeatureSignal]]:
        selected: dict[str, FeatureSignal] = {}
        suppressed: list[FeatureSignal] = []
        for signal in signals:
            if signal.direction == 'neutral' or signal.strength <= 0:
                suppressed.append(signal)
                continue
            current = selected.get(signal.family)
            current_score = current.weight * current.strength if current is not None else -1.0
            signal_score = signal.weight * signal.strength
            if current is None or signal_score > current_score:
                if current is not None:
                    suppressed.append(current)
                selected[signal.family] = signal
            else:
                suppressed.append(signal)
        return list(selected.values()), suppressed

    @staticmethod
    def _build_reasoning(
        *,
        action: str,
        used_signals: list[FeatureSignal],
        suppressed_signals: list[FeatureSignal],
        blocked_reasons: tuple[str, ...],
    ) -> str:
        if used_signals:
            used_summary = ', '.join(
                f'{signal.name}:{signal.direction}:{signal.weight * signal.strength:.3f}'
                for signal in used_signals
            )
        else:
            used_summary = 'none'

        parts = [f'action={action}', f'used={used_summary}']
        if suppressed_signals:
            suppressed_summary = ', '.join(signal.name for signal in suppressed_signals)
            parts.append(f'suppressed={suppressed_summary}')
        if blocked_reasons:
            parts.append(f'blocked={";".join(blocked_reasons)}')
        return ' | '.join(parts)
