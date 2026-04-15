from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
MODULES = SKILL_ROOT / 'modules'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))

from btc_sprint_executor import _side_price, execute_trade


@dataclass
class DummySignal:
    action: str = 'yes'
    edge: float = 0.12
    confidence: float = 0.8
    reasoning: str = 'dummy reasoning'

    def to_signal_data(self) -> dict:
        return {
            'signal_source': 'binance_1m_momentum',
            'edge': self.edge,
            'confidence': self.confidence,
        }


class RecordingClient:
    def __init__(self) -> None:
        self.trade_calls = 0

    def trade(self, **_kwargs):  # pragma: no cover - defensive only
        self.trade_calls += 1
        raise AssertionError('trade() should not be called when current_probability is unavailable')


def test_side_price_rounds_to_cent_ticks_for_yes_and_no():
    assert _side_price('yes', {'market': {'current_probability': 0.191}}) == 0.19
    assert _side_price('no', {'market': {'current_probability': 0.809}}) == 0.19


def test_execute_trade_blocks_before_submission_when_current_probability_missing():
    client = RecordingClient()
    signal = DummySignal()
    result = execute_trade(
        client,
        market_id='m1',
        side='yes',
        amount=4.0,
        signal=signal,
        regime={'warnings': [], 'reasons': []},
        live=True,
        source='btc-sprint-stack',
        skill_slug='btc-sprint-stack',
        venue='polymarket',
        validate_real_path=False,
        context={'market': {}},
    )

    assert client.trade_calls == 0
    assert result['result_type'] == 'dry_run'
    assert result['blocked'] is True
    assert result['block_reason'] == 'cannot_verify_minimum_shares:current_probability_unavailable'
    assert result['pre_submit_guard'] == {
        'guard_skipped': True,
        'reason': 'current_probability_unavailable',
    }

