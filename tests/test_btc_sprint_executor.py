from __future__ import annotations

import sys
from dataclasses import dataclass
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
MODULES = SKILL_ROOT / 'modules'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))

from btc_sprint_executor import _side_price, execute_trade
from btc_regime_filter import evaluate_regime


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
        self.preflight_calls = 0

    def trade(self, **_kwargs):  # pragma: no cover - defensive only
        self.trade_calls += 1
        raise AssertionError('trade() should not be called when current_probability is unavailable')

    def prepare_real_trade(self, *_args, **_kwargs):
        self.preflight_calls += 1
        return SimpleNamespace(success=True)


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


def test_live_polymarket_passes_guarded_price_to_trade():
    """client.trade() must receive the pre-computed rounded guard price, not re-fetch."""
    captured = {}

    class CapturingClient(RecordingClient):
        def trade(self, **kwargs):
            self.trade_calls += 1
            captured.update(kwargs)
            return SimpleNamespace(
                order_status='matched', success=True, cost=4.0,
                new_price=0.49, shares_bought=8.16, shares_requested=8.16,
                side=kwargs.get('side'), market_id=kwargs.get('market_id'),
                trade_id='t1', error=None, skip_reason=None, simulated=False,
                balance=None,
            )

    client = CapturingClient()
    signal = DummySignal()
    execute_trade(
        client,
        market_id='m1',
        side='no',
        amount=4.0,
        signal=signal,
        regime={'warnings': [], 'reasons': []},
        live=True,
        source='btc-sprint-stack',
        skill_slug='btc-sprint-stack',
        venue='polymarket',
        validate_real_path=False,
        context={'market': {'current_probability': 0.51}},
    )

    assert client.trade_calls == 1
    # current_probability=0.51 → NO price = round(1-0.51, 2) = 0.49
    assert captured.get('price') == 0.49


def test_non_polymarket_live_path_does_not_pass_price():
    """Non-polymarket venues must not receive a price kwarg (no guard runs)."""
    captured = {}

    class CapturingClient(RecordingClient):
        def trade(self, **kwargs):
            self.trade_calls += 1
            captured.update(kwargs)
            return SimpleNamespace(
                order_status='matched', success=True, cost=4.0,
                new_price=0.49, shares_bought=8.0, shares_requested=8.0,
                side=kwargs.get('side'), market_id=kwargs.get('market_id'),
                trade_id='t2', error=None, skip_reason=None, simulated=False,
                balance=None,
            )

    client = CapturingClient()
    signal = DummySignal()
    execute_trade(
        client,
        market_id='m2',
        side='yes',
        amount=4.0,
        signal=signal,
        regime={'warnings': [], 'reasons': []},
        live=True,
        source='btc-sprint-stack',
        skill_slug='btc-sprint-stack',
        venue='kalshi',
        validate_real_path=False,
        context={'market': {'current_probability': 0.51}},
    )

    assert client.trade_calls == 1
    assert captured.get('price') is None


@pytest.mark.parametrize(
    'preflight, expected_reason',
    [
        (
            SimpleNamespace(
                success=False,
                error='Already hold position on this market (source: btc-sprint-stack). Pass allow_rebuy=True to override.',
                skip_reason='rebuy skipped',
            ),
            'Already hold position on this market (source: btc-sprint-stack). Pass allow_rebuy=True to override.',
        ),
        (
            SimpleNamespace(
                success=False,
                error='Order too small: 4.65 shares after rounding is below minimum (5)',
                skip_reason=None,
            ),
            'Order too small: 4.65 shares after rounding is below minimum (5)',
        ),
    ],
)
def test_execute_trade_blocks_when_live_preflight_reports_rebuy_or_min_size(preflight, expected_reason):
    class PreflightClient(RecordingClient):
        def __init__(self, preflight_result) -> None:
            super().__init__()
            self.preflight_result = preflight_result

        def prepare_real_trade(self, *_args, **_kwargs):
            self.preflight_calls += 1
            return self.preflight_result

    client = PreflightClient(preflight)
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
        validate_real_path=True,
        context={'market': {'current_probability': 0.51}},
    )

    assert client.preflight_calls == 1
    assert client.trade_calls == 0
    assert result['result_type'] == 'dry_run'
    assert result['blocked'] is True
    assert result['block_reason'] == expected_reason
    assert result['preflight']['success'] is False


# --- evaluate_regime edge gate tests ---

def _regime_signal(edge: float, confidence: float = 0.70, action: str = 'yes'):
    return SimpleNamespace(edge=edge, confidence=confidence, action=action)


def _market(fee_rate_bps: int = 1000, resolves_in_minutes: float = 10.0):
    from datetime import datetime, timezone, timedelta
    resolves_at = (datetime.now(timezone.utc) + timedelta(minutes=resolves_in_minutes)).isoformat()
    return {
        'fee_rate_bps': fee_rate_bps,
        'resolves_at': resolves_at,
        'current_probability': 0.5,
    }


def test_regime_blocks_edge_below_fee_rate_when_min_edge_is_zero():
    """live_params min_edge=0.0 must not override the fee-rate floor."""
    config = {'min_edge': 0.0, 'min_confidence': 0.60, 'max_slippage_pct': 0.05}
    context = {'market': _market(fee_rate_bps=1000), 'slippage': {'spread_pct': 0.01}}
    signal = _regime_signal(edge=0.025)  # well below fee_rate=0.10
    result = evaluate_regime(context, signal, config)
    assert result['approved'] is False
    assert any('edge_not_above_fee' in r for r in result['reasons'])


def test_regime_allows_edge_above_fee_rate():
    """An edge that clears both fee_rate and min_edge must be approved."""
    config = {'min_edge': 0.0, 'min_confidence': 0.60, 'max_slippage_pct': 0.05}
    context = {'market': _market(fee_rate_bps=1000), 'slippage': {'spread_pct': 0.01}}
    signal = _regime_signal(edge=0.12)  # above fee_rate=0.10
    result = evaluate_regime(context, signal, config)
    assert result['approved'] is True
    assert not any('edge_not_above_fee' in r for r in result['reasons'])


def test_regime_uses_config_min_edge_when_it_exceeds_fee_rate():
    """If config min_edge > fee_rate, config min_edge is the binding floor."""
    config = {'min_edge': 0.15, 'min_confidence': 0.60, 'max_slippage_pct': 0.05}
    context = {'market': _market(fee_rate_bps=500), 'slippage': {'spread_pct': 0.01}}
    # fee_rate=0.05, but min_edge=0.15 — edge=0.08 clears fee but not min_edge
    signal = _regime_signal(edge=0.08)
    result = evaluate_regime(context, signal, config)
    assert result['approved'] is False
    assert any('edge_not_above_fee' in r for r in result['reasons'])


def test_regime_edge_gate_inactive_when_fee_rate_and_min_edge_both_zero():
    """If fee_rate=0 and min_edge=0, no edge rejection fires (no floor to enforce)."""
    config = {'min_edge': 0.0, 'min_confidence': 0.60, 'max_slippage_pct': 0.05}
    context = {'market': _market(fee_rate_bps=0), 'slippage': {'spread_pct': 0.01}}
    signal = _regime_signal(edge=0.001)
    result = evaluate_regime(context, signal, config)
    assert not any('edge_not_above_fee' in r for r in result['reasons'])
