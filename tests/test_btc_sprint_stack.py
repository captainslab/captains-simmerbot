from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))

from btc_position_manager import enforce_risk_limits
from btc_regime_filter import evaluate_regime
from btc_sprint_signal import build_signal
from btc_heartbeat import build_heartbeat
from btc_trade_journal import reconcile_journal_outcomes


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
    assert defaults['max_trades_per_hour'] == 12
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


def test_position_manager_counts_sdk_positions_with_sources():
    config = json.loads((ROOT / 'skills' / 'btc-sprint-stack' / 'config' / 'defaults.json').read_text())
    positions = [
        SimpleNamespace(sources=['btc-sprint-stack'], shares_yes=1.0, shares_no=0.0),
        SimpleNamespace(sources=['other-skill'], shares_yes=2.0, shares_no=0.0),
    ]
    verdict = enforce_risk_limits({'sdk_daily_spent': 0, 'trading_paused': False}, positions, config, 'btc-sprint-stack', [])
    assert verdict['open_positions'] == 1
    assert verdict['allowed'] is True


def test_position_manager_blocks_after_twelve_trades_in_last_hour():
    config = json.loads((ROOT / 'skills' / 'btc-sprint-stack' / 'config' / 'defaults.json').read_text())
    now = datetime.now(timezone.utc)
    journal_rows = [
        {
            'ts': (now - timedelta(minutes=index * 4)).isoformat(),
            'result_type': 'trade',
        }
        for index in range(12)
    ]
    verdict = enforce_risk_limits(
        {'sdk_daily_spent': 0, 'trading_paused': False},
        [],
        config,
        'btc-sprint-stack',
        journal_rows,
    )
    assert verdict['allowed'] is False
    assert 'max_trades_per_hour_reached' in verdict['reasons']


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


# --- daily_spent journal-derived tests ---

def _base_config():
    cfg = json.loads((ROOT / 'skills' / 'btc-sprint-stack' / 'config' / 'defaults.json').read_text())
    return cfg


def _trade_row(ts: datetime, amount: float) -> dict:
    return {
        'result_type': 'trade',
        'ts': ts.isoformat(),
        'risk_state': {'trade_amount_usd': amount},
    }


def test_daily_spent_counts_todays_trade_rows():
    config = _base_config()
    config['max_daily_loss_usd'] = 100  # high cap so we only measure daily_spent value
    now = datetime.now(timezone.utc)
    rows = [_trade_row(now, 4.0), _trade_row(now, 4.0)]
    verdict = enforce_risk_limits({}, [], config, 'btc-sprint-stack', rows)
    assert verdict['daily_spent'] == 8.0


def test_daily_spent_ignores_non_trade_rows():
    config = _base_config()
    config['max_daily_loss_usd'] = 100
    now = datetime.now(timezone.utc)
    rows = [
        {'result_type': 'skip', 'ts': now.isoformat(), 'risk_state': {'trade_amount_usd': 4.0}},
        {'result_type': 'dry_run', 'ts': now.isoformat(), 'risk_state': {'trade_amount_usd': 4.0}},
    ]
    verdict = enforce_risk_limits({}, [], config, 'btc-sprint-stack', rows)
    assert verdict['daily_spent'] == 0.0


def test_daily_spent_ignores_prior_day_rows():
    config = _base_config()
    config['max_daily_loss_usd'] = 100
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    rows = [_trade_row(yesterday, 4.0), _trade_row(yesterday, 4.0)]
    verdict = enforce_risk_limits({}, [], config, 'btc-sprint-stack', rows)
    assert verdict['daily_spent'] == 0.0


def test_daily_spent_triggers_cap_when_limit_reached():
    config = _base_config()
    config['max_daily_loss_usd'] = 10
    now = datetime.now(timezone.utc)
    # 3 trades × $4 = $12 >= $10 cap
    rows = [_trade_row(now, 4.0) for _ in range(3)]
    verdict = enforce_risk_limits({}, [], config, 'btc-sprint-stack', rows)
    assert verdict['daily_spent'] == 12.0
    assert verdict['allowed'] is False
    assert 'max_daily_loss_reached' in verdict['reasons']


# --- reconcile_journal_outcomes tests ---

def _make_pos(market_id, status='active', pnl=0.0):
    return SimpleNamespace(market_id=market_id, status=status, pnl=pnl)


def _make_trade_row(market_id):
    return {'result_type': 'trade', 'market_id': market_id, 'ts': datetime.now(timezone.utc).isoformat()}


def test_reconcile_writes_win_for_resolved_position(tmp_path):
    journal_path = tmp_path / 'journal.jsonl'
    rows = [_make_trade_row('mkt-1')]
    positions = [_make_pos('mkt-1', status='resolved', pnl=3.50)]
    result = reconcile_journal_outcomes(journal_path, rows, positions)
    assert result[0]['pnl_usd'] == 3.50
    assert result[0]['outcome'] == 'win'
    assert journal_path.exists()


def test_reconcile_writes_loss_with_negative_pnl(tmp_path):
    journal_path = tmp_path / 'journal.jsonl'
    rows = [_make_trade_row('mkt-2')]
    positions = [_make_pos('mkt-2', status='resolved', pnl=-4.0)]
    result = reconcile_journal_outcomes(journal_path, rows, positions)
    assert result[0]['pnl_usd'] == -4.0
    assert result[0]['outcome'] == 'loss'


def test_reconcile_leaves_unresolved_rows_unchanged(tmp_path):
    journal_path = tmp_path / 'journal.jsonl'
    rows = [_make_trade_row('mkt-3')]
    positions = [_make_pos('mkt-3', status='active', pnl=1.0)]
    result = reconcile_journal_outcomes(journal_path, rows, positions)
    assert 'pnl_usd' not in result[0]
    assert not journal_path.exists()  # no update means no rewrite


def test_reconcile_loss_visible_to_cooldown_logic(tmp_path):
    journal_path = tmp_path / 'journal.jsonl'
    rows = [_make_trade_row('mkt-4')]
    positions = [_make_pos('mkt-4', status='resolved', pnl=-4.0)]
    result = reconcile_journal_outcomes(journal_path, rows, positions)
    config = _base_config()
    config['cooldown_after_loss_minutes'] = 60
    verdict = enforce_risk_limits({}, [], config, 'btc-sprint-stack', result)
    assert any('cooldown_active' in r for r in verdict['reasons'])
