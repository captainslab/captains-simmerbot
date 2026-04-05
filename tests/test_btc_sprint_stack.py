from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))

from btc_position_manager import enforce_risk_limits
from btc_regime_filter import evaluate_regime
from btc_sprint_signal import build_signal
from btc_heartbeat import build_heartbeat


class DummySignal:
    def __init__(self, action='yes', edge=0.12, confidence=0.8):
        self.action = action
        self.edge = edge
        self.confidence = confidence
        self.reasoning = 'dummy'


def test_defaults_match_required_values():
    defaults = json.loads((ROOT / 'skills' / 'btc-sprint-stack' / 'config' / 'defaults.json').read_text())
    assert defaults['bankroll_usd'] == 60
    assert defaults['max_trade_usd'] == 4
    assert defaults['max_daily_loss_usd'] == 10
    assert defaults['max_open_positions'] == 2
    assert defaults['max_single_market_exposure_usd'] == 8
    assert defaults['max_trades_per_day'] == 6
    assert defaults['min_edge'] == 0.07
    assert defaults['min_confidence'] == 0.65
    assert defaults['max_slippage_pct'] == 0.1
    assert defaults['stop_loss_pct'] == 0.1
    assert defaults['take_profit_pct'] == 0.12
    assert defaults['cooldown_after_loss_minutes'] == 60
    assert defaults['cycle_interval_minutes'] == 15


def test_regime_filter_rejects_edge_below_fee():
    config = json.loads((ROOT / 'skills' / 'btc-sprint-stack' / 'config' / 'defaults.json').read_text())
    context = {
        'market': {
            'resolves_at': '2099-01-01T00:10:00+00:00',
            'fee_rate_bps': 1000,
        },
        'slippage': {'spread_pct': 0.02},
        'warnings': [],
    }
    signal = DummySignal(edge=0.08, confidence=0.8)
    verdict = evaluate_regime(context, signal, config)
    assert verdict['approved'] is False
    assert any('edge_not_above_fee' in reason for reason in verdict['reasons'])


def test_position_manager_caps_trade_amount_and_blocks_on_open_positions():
    config = json.loads((ROOT / 'skills' / 'btc-sprint-stack' / 'config' / 'defaults.json').read_text())
    positions = [
        {'source': 'btc-sprint-stack', 'shares': 1},
        {'source': 'btc-sprint-stack', 'shares': 1},
    ]
    verdict = enforce_risk_limits({'sdk_daily_spent': 0, 'trading_paused': False}, positions, config, 'btc-sprint-stack', [])
    assert verdict['trade_amount_usd'] == 4
    assert verdict['allowed'] is False
    assert 'max_open_positions_reached' in verdict['reasons']


def test_build_heartbeat_degrades_when_briefing_times_out():
    class DummyClient:
        def get_briefing(self):
            raise TimeoutError('briefing timed out')

    heartbeat = build_heartbeat(
        DummyClient(),
        decisions=[{'decision': 'candidate'}, {'decision': 'skipped'}],
        risk_state={'allowed': True},
        learning_snapshot={'candidate_count': 0},
    )

    assert heartbeat['briefing'] is None
    assert heartbeat['warning'] == {
        'code': 'briefing_unavailable',
        'message': 'briefing timed out',
        'type': 'TimeoutError',
    }
    assert heartbeat['decision_count'] == 2
    assert heartbeat['accepted_candidates'] == 1
