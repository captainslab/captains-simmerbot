from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from btc_llm_decider import (  # noqa: E402
    MAX_MODEL_OUTPUT_TOKENS,
    MissingLLMCredentialsError,
    build_compact_user_prompt,
    build_provider_from_env,
    parse_model_output,
    validate_model_output,
)
from btc_position_manager import enforce_risk_limits  # noqa: E402
from btc_regime_filter import evaluate_regime  # noqa: E402
import main as btc_main  # type: ignore  # noqa: E402


def test_parse_model_output_rejects_malformed_json():
    with pytest.raises(Exception, match='malformed JSON'):
        parse_model_output('not json')


def test_validate_model_output_rejects_non_btc_and_extra_keys():
    with pytest.raises(Exception, match='asset must be BTC'):
        validate_model_output(
            {
                'asset': 'ETH',
                'action': 'yes',
                'confidence': 0.7,
                'edge': 0.2,
                'reasoning': 'bad asset',
            }
        )

    with pytest.raises(Exception, match='unexpected keys'):
        validate_model_output(
            {
                'asset': 'BTC',
                'action': 'yes',
                'confidence': 0.7,
                'edge': 0.2,
                'reasoning': 'ok',
                'extra': True,
            }
        )


def test_build_provider_from_env_requires_credentials(monkeypatch):
    for key in ('LLM_PROVIDER', 'LLM_MODEL', 'LLM_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_API_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(MissingLLMCredentialsError, match='missing LLM provider credentials'):
        build_provider_from_env({})


def test_build_provider_from_env_supports_generic_deepseek_contract():
    provider = build_provider_from_env(
        {
            'LLM_PROVIDER': 'deepseek',
            'LLM_API_KEY': 'test-key',
        }
    )
    assert provider.provider_name == 'deepseek'
    assert provider.model_name == 'deepseek-chat'
    assert provider.base_url == 'https://api.deepseek.com'


def test_build_provider_from_env_supports_openrouter_compatible_contract():
    provider = build_provider_from_env(
        {
            'LLM_PROVIDER': 'openrouter',
            'LLM_API_KEY': 'test-key',
            'LLM_MODEL': 'google/gemini-2.5-pro',
        }
    )
    assert provider.provider_name == 'openrouter'
    assert provider.model_name == 'google/gemini-2.5-pro'
    assert provider.base_url == 'https://openrouter.ai/api/v1'


def test_build_provider_from_env_supports_google_api_key_contract():
    provider = build_provider_from_env(
        {
            'LLM_PROVIDER': 'google',
            'GOOGLE_API_KEY': 'test-key',
            'LLM_MODEL': 'google/gemini-2.5-flash',
        }
    )
    assert provider.provider_name == 'google'
    assert provider.model_name == 'google/gemini-2.5-flash'
    assert provider.base_url == 'https://generativelanguage.googleapis.com/v1beta/openai'


def test_compact_user_prompt_omits_pending_rules_and_is_short():
    prompt = build_compact_user_prompt(
        market_context={
            'market_id': 'btc-fast-1',
            'question': 'Bitcoin Up or Down - April 5, 8:55PM-9:00PM ET',
            'window': '5m',
            'asset': 'BTC',
            'venue': 'polymarket',
            'resolves_at': '2099-01-01T00:10:00+00:00',
            'fee_rate_bps': 100,
            'spread_pct': 0.02,
            'warnings': ['one', 'two', 'three'],
        },
        signal={
            'edge': 0.12,
            'confidence': 0.9,
            'signal_source': 'unit-test',
            'window': '5m',
            'short_move': 0.04,
            'long_move': 0.08,
            'volatility': 0.03,
        },
        regime={
            'approved': True,
            'reasons': ['ok', 'still-ok', 'extra'],
            'warnings': ['warn-a', 'warn-b'],
            'minutes_to_resolution': 12,
            'spread_pct': 0.02,
            'fee_rate': 100,
        },
        risk_state={
            'allowed': True,
            'reasons': ['risk-ok', 'risk-extra'],
            'trade_amount_usd': 4,
            'open_positions': 0,
            'daily_spent': 0,
            'execution_mode': 'dry_run',
        },
        live_params={
            'min_edge': 0.07,
            'min_confidence': 0.65,
            'max_slippage_pct': 0.1,
            'cycle_interval_minutes': 3,
            'stop_loss_pct': 0.1,
            'take_profit_pct': 0.12,
        },
        pending_rules={
            'rules': [{'key': 'min_edge', 'value': '0.08'}],
        },
        learning_snapshot={
            'candidate_count': 20,
            'avg_edge': 0.09,
            'avg_confidence': 0.71,
            'pending_rule_count': 1,
        },
    )
    parsed = json.loads(prompt)
    assert 'pending_rules' not in parsed
    assert 'market' in parsed
    assert 'gate' in parsed
    assert len(prompt) < 900


def test_max_model_output_tokens_capped():
    assert MAX_MODEL_OUTPUT_TOKENS <= 200


def test_load_config_merges_live_params_before_env_overrides(tmp_path, monkeypatch):
    defaults_path = tmp_path / 'defaults.json'
    live_params_path = tmp_path / 'live_params.json'
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
                'max_trades_per_day': 6,
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
    live_params_path.write_text(json.dumps({'cycle_interval_minutes': 20, 'min_edge': 0.05}))
    monkeypatch.setenv('BTC_SPRINT_PROFILE', 'aggressive')
    config = btc_main.load_config(defaults_path=defaults_path, live_params_path=live_params_path)
    assert config['cycle_interval_minutes'] == 10
    assert config['min_edge'] == 0.05
    assert config['asset'] == 'BTC'
    assert config['trading_venue'] == 'polymarket'


def test_deterministic_gate_blocks_model_yes_when_risk_rejects():
    config = json.loads((ROOT / 'skills' / 'btc-sprint-stack' / 'config' / 'defaults.json').read_text())
    context = {
        'market': {
            'resolves_at': '2099-01-01T00:10:00+00:00',
            'fee_rate_bps': 100,
        },
        'slippage': {'spread_pct': 0.02},
        'warnings': [],
    }
    class Signal:
        action = 'yes'
        edge = 0.12
        confidence = 0.9
        reasoning = 'model says yes'

        def to_signal_data(self):
            return {'edge': 0.12, 'confidence': 0.9, 'signal_source': 'unit-test'}

    signal = Signal()
    regime = evaluate_regime(context, signal, config)
    risk_state = enforce_risk_limits(
        {'sdk_daily_spent': 0, 'trading_paused': False},
        [{'source': 'btc-sprint-stack', 'shares': 1}, {'source': 'btc-sprint-stack', 'shares': 1}],
        config,
        'btc-sprint-stack',
        [],
        execution_mode='dry_run',
        regime=regime,
    )
    assert regime['approved'] is True
    assert risk_state['allowed'] is False
    model_yes = {'asset': 'BTC', 'action': 'yes', 'confidence': 0.9, 'edge': 0.2, 'reasoning': 'take trade'}
    should_execute = bool(model_yes and model_yes['action'] in {'yes', 'no'} and regime['approved'] and risk_state['allowed'])
    assert should_execute is False
