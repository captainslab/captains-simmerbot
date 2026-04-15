from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from btc_discord_control import (  # noqa: E402
    apply_control_update,
    load_control_state,
    parse_control_message,
    summarize_control_state,
)
import main as btc_main  # type: ignore  # noqa: E402


def test_parse_control_message_supports_profile_and_tunables():
    update = parse_control_message('be more aggressive and set min edge to 0.08')
    assert update is not None
    assert update.execution_profile == 'aggressive'
    assert update.live_overrides['min_edge'] == 0.08


def test_parse_control_message_supports_strategy_labels_and_skill_tags():
    update = parse_control_message('call the strategy breakout and add momentum skill')
    assert update is not None
    assert update.strategy_label == 'breakout'
    assert update.skill_tags == ['momentum']


def test_apply_control_update_can_remove_skill_tags():
    update = parse_control_message('remove momentum skill')
    assert update is not None
    state = apply_control_update({'skill_tags': ['momentum', 'mean-reversion']}, update)
    assert state['skill_tags'] == ['mean-reversion']


def test_parse_control_message_supports_full_risk_transforms():
    update = parse_control_message('make it more aggressive, set max trade to 6 dollars, allow 3 open positions, and set cooldown after loss to 30 minutes')
    assert update is not None
    assert update.execution_profile == 'aggressive'
    assert update.live_overrides['max_trade_usd'] == 6.0
    assert update.live_overrides['max_open_positions'] == 3
    assert update.live_overrides['cooldown_after_loss_minutes'] == 30


def test_parse_control_message_supports_hourly_trade_caps():
    update = parse_control_message('cap trades per hour to 12')
    assert update is not None
    assert update.live_overrides['max_trades_per_hour'] == 12


def test_load_control_state_defaults_to_neutral_state(tmp_path):
    state = load_control_state(tmp_path / 'discord_control_state.json')
    assert state['execution_profile'] is None
    assert state['strategy_label'] is None
    assert state['skill_tags'] == []


def test_load_config_applies_discord_control_state(tmp_path):
    defaults_path = tmp_path / 'defaults.json'
    live_params_path = tmp_path / 'live_params.json'
    discord_state_path = tmp_path / 'discord_control_state.json'
    defaults_path.write_text(
        json.dumps(
            {
                'skill_slug': 'btc-sprint-stack',
                'asset': 'BTC',
                'windows': ['5m', '15m'],
                'signal_source': 'binance_btcusdt_1m',
                'bankroll_usd': 60,
                'max_trade_usd': 4,
                'max_daily_loss_usd': 10,
                'max_open_positions': 2,
                'max_single_market_exposure_usd': 8,
                'max_trades_per_hour': 12,
                'min_edge': 0.07,
                'min_confidence': 0.65,
                'max_slippage_pct': 0.1,
                'stop_loss_pct': 0.1,
                'take_profit_pct': 0.12,
                'cooldown_after_loss_minutes': 60,
                'cycle_interval_minutes': 15,
                'trading_venue': 'polymarket',
                'validate_real_path': True,
            }
        )
    )
    live_params_path.write_text(json.dumps({'min_edge': 0.06}))
    discord_state_path.write_text(
        json.dumps(
            {
                'execution_profile': 'aggressive',
                'strategy_label': 'breakout',
                'skill_tags': ['momentum'],
                'live_overrides': {
                    'min_edge': 0.09,
                    'cycle_interval_minutes': 9,
                    'max_trade_usd': 6.0,
                    'max_open_positions': 3,
                },
            }
        )
    )

    config = btc_main.load_config(
        defaults_path=defaults_path,
        live_params_path=live_params_path,
        discord_control_path=discord_state_path,
    )

    assert config['execution_profile'] == 'aggressive'
    assert config['min_edge'] == 0.09
    assert config['cycle_interval_minutes'] == 9
    assert config['max_trade_usd'] == 6.0
    assert config['max_open_positions'] == 3
    assert config['discord_strategy_label'] == 'breakout'
    assert config['discord_skill_tags'] == ['momentum']
    assert config['asset'] == 'BTC'


def test_summarize_control_state_includes_active_overrides():
    summary = summarize_control_state(
        {
            'execution_profile': 'aggressive',
            'strategy_label': 'breakout',
            'skill_tags': ['momentum'],
            'live_overrides': {'min_edge': 0.08},
        }
    )
    assert 'profile=aggressive' in summary
    assert 'strategy=breakout' in summary
    assert 'skills=momentum' in summary
    assert 'min_edge=0.08' in summary
