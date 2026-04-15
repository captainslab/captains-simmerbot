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

import btc_llm_decider as llm_decider
from btc_llm_decider import (  # noqa: E402
    InvalidLLMOutputError,
    MAX_MODEL_OUTPUT_TOKENS,
    MAX_USER_PROMPT_CHARS,
    MissingLLMCredentialsError,
    OpenAIResponsesProvider,
    ProviderRequestError,
    build_compact_user_prompt,
    build_provider_from_env,
    parse_model_output,
    run_llm_decision,
    validate_model_output,
)
from btc_position_manager import enforce_risk_limits  # noqa: E402
from btc_regime_filter import evaluate_regime  # noqa: E402
import main as btc_main  # type: ignore  # noqa: E402


def test_parse_model_output_rejects_malformed_json():
    with pytest.raises(Exception, match='malformed JSON'):
        parse_model_output('not json')


def test_parse_model_output_accepts_fenced_json():
    payload = parse_model_output(
        '```json\n{"asset":"BTC","action":"skip","confidence":0.5,"edge":0.1,"reasoning":"ok"}\n```'
    )
    assert payload['asset'] == 'BTC'
    assert payload['action'] == 'skip'


def test_parse_model_output_extracts_wrapped_json_object():
    payload = parse_model_output(
        'Here is the decision:\n{"asset":"BTC","action":"skip","confidence":0.5,"edge":0.1,"reasoning":"ok"}'
    )
    assert payload['asset'] == 'BTC'
    assert payload['action'] == 'skip'


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


def test_build_provider_from_env_defaults_google_to_gemini_flash():
    provider = build_provider_from_env(
        {
            'LLM_PROVIDER': 'google',
            'GOOGLE_API_KEY': 'test-key',
        }
    )
    assert provider.provider_name == 'google'
    assert provider.model_name == 'gemini-2.5-flash'
    assert provider.base_url == 'https://generativelanguage.googleapis.com/v1beta/openai'


def test_google_oauth_provider_uses_vertex_openai_endpoint(monkeypatch):
    class FakeCredentials:
        def __init__(self):
            self.token = ''

        def refresh(self, request):
            self.token = 'oauth-token'

    class FakeRequest:
        pass

    fake_google_auth = type(
        'FakeGoogleAuthModule',
        (),
        {
            'default': staticmethod(lambda scopes=None: (FakeCredentials(), 'vertex-project')),
            'transport': None,
        },
    )
    fake_google_auth_requests = type(
        'FakeGoogleAuthRequestsModule',
        (),
        {
            'Request': FakeRequest,
        },
    )
    fake_google_auth_transport = type(
        'FakeGoogleAuthTransportModule',
        (),
        {
            'requests': fake_google_auth_requests,
        },
    )
    fake_google = type(
        'FakeGoogleModule',
        (),
        {
            'auth': fake_google_auth,
        },
    )
    monkeypatch.setitem(sys.modules, 'google', fake_google)
    monkeypatch.setitem(sys.modules, 'google.auth', fake_google_auth)
    monkeypatch.setitem(sys.modules, 'google.auth.transport', fake_google_auth_transport)
    monkeypatch.setitem(sys.modules, 'google.auth.transport.requests', fake_google_auth_requests)
    fake_google_auth.transport = fake_google_auth_transport

    captured: dict[str, str] = {}

    class DummyDelegate:
        def __init__(self, *, provider_name, api_key, model_name, base_url):
            captured['provider_name'] = provider_name
            captured['api_key'] = api_key
            captured['model_name'] = model_name
            captured['base_url'] = base_url

        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            captured['system_prompt'] = system_prompt
            captured['user_prompt'] = user_prompt
            return '{"asset":"BTC","action":"skip","confidence":0.5,"edge":0.0,"reasoning":"ok"}'

    monkeypatch.setattr(llm_decider, 'OpenAIResponsesProvider', DummyDelegate)

    provider = build_provider_from_env(
        {
            'LLM_PROVIDER': 'google_oauth',
            'LLM_MODEL': 'gemini-2.5-pro',
            'GOOGLE_CLOUD_LOCATION': 'global',
        }
    )
    result = provider.complete(system_prompt='system', user_prompt='user')

    assert result.startswith('{"asset":"BTC"')
    assert captured['provider_name'] == 'google_oauth'
    assert captured['api_key'] == 'oauth-token'
    assert captured['model_name'] == 'gemini-2.5-pro'
    assert captured['base_url'] == 'https://aiplatform.googleapis.com/v1/projects/vertex-project/locations/global/endpoints/openapi'


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
            'action': 'yes',
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
    assert parsed['signal']['action'] == 'yes'
    assert len(prompt) < 900


def test_compact_user_prompt_clamps_oversized_question():
    prompt = build_compact_user_prompt(
        market_context={
            'market_id': 'btc-fast-1',
            'question': 'Bitcoin?' * 400,
            'window': '5m',
            'asset': 'BTC',
            'venue': 'polymarket',
            'resolves_at': '2099-01-01T00:10:00+00:00',
            'fee_rate_bps': 100,
            'spread_pct': 0.02,
            'warnings': [],
        },
        signal={
            'action': 'yes',
            'edge': 0.12,
            'confidence': 0.9,
            'signal_source': 'unit-test',
            'window': '5m',
        },
        regime={
            'approved': True,
            'reasons': ['ok'],
            'warnings': [],
            'minutes_to_resolution': 12,
            'spread_pct': 0.02,
            'fee_rate': 100,
        },
        risk_state={
            'allowed': True,
            'reasons': ['risk-ok'],
            'trade_amount_usd': 4,
            'open_positions': 0,
            'daily_spent': 0,
            'execution_mode': 'dry_run',
        },
        live_params={
            'min_edge': 0.07,
            'min_confidence': 0.65,
            'max_slippage_pct': 0.1,
        },
        pending_rules={'rules': []},
        learning_snapshot={
            'avg_edge': 0.09,
            'avg_confidence': 0.71,
        },
    )
    assert len(prompt) <= MAX_USER_PROMPT_CHARS


def test_max_model_output_tokens_capped():
    assert MAX_MODEL_OUTPUT_TOKENS <= 200


def test_openai_provider_falls_back_when_schema_request_is_rejected(monkeypatch):
    calls: list[dict] = []

    def fake_post_json(url, *, body, headers):
        del url, headers
        calls.append(body)
        if 'response_format' in body:
            raise ProviderRequestError('provider request failed: 400 response_format unsupported')
        return {
            'choices': [
                {
                    'message': {
                        'content': '{"asset":"BTC","action":"yes","confidence":0.72,"edge":0.11,"reasoning":"ok"}'
                    }
                }
            ]
        }

    monkeypatch.setattr(llm_decider, '_post_json', fake_post_json)
    provider = OpenAIResponsesProvider(provider_name='openrouter', api_key='test-key', model_name='openrouter/free')

    result = provider.complete(system_prompt='system', user_prompt='user')

    assert json.loads(result)['action'] == 'yes'
    assert 'response_format' in calls[0]
    assert 'response_format' not in calls[1]


def test_extract_response_text_accepts_nested_provider_text_blocks():
    raw = {
        'choices': [
            {
                'message': {
                    'content': [
                        {
                            'type': 'text',
                            'text': {
                                'value': '{"asset":"BTC","action":"yes","confidence":0.72,"edge":0.11,"reasoning":"ok"}'
                            },
                        }
                    ]
                }
            }
        ]
    }

    assert json.loads(llm_decider._extract_response_text(raw))['action'] == 'yes'


def test_extract_response_text_accepts_legacy_choice_text():
    raw = {
        'choices': [
            {
                'text': '{"asset":"BTC","action":"no","confidence":0.72,"edge":0.11,"reasoning":"ok"}'
            }
        ]
    }

    assert json.loads(llm_decider._extract_response_text(raw))['action'] == 'no'


def test_openai_provider_rejects_empty_text_content_cleanly(monkeypatch):
    def fake_post_json(url, *, body, headers):
        del url, body, headers
        return {
            'choices': [
                {
                    'message': {
                        'content': [
                            {
                                'type': 'text',
                                'text': {'value': '   '},
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr(llm_decider, '_post_json', fake_post_json)
    provider = OpenAIResponsesProvider(provider_name='openrouter', api_key='test-key', model_name='openrouter/free')

    with pytest.raises(ProviderRequestError, match='provider returned empty text content'):
        provider.complete(system_prompt='system', user_prompt='user')


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


def test_execution_side_must_match_signal_direction():
    side, reject_reason = btc_main._resolve_execution_side('no', {'action': 'yes'})
    assert side is None
    assert reject_reason == 'side_mismatch:signal=no,llm=yes'

    aligned_side, aligned_reason = btc_main._resolve_execution_side('yes', {'action': 'yes'})
    assert aligned_side == 'yes'
    assert aligned_reason is None


def test_run_llm_decision_rejects_opposite_side_to_signal():
    class StubProvider:
        provider_name = 'stub'
        model_name = 'stub-model'

        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            del system_prompt, user_prompt
            return json.dumps({
                'asset': 'BTC',
                'action': 'yes',
                'confidence': 0.7,
                'edge': 0.1,
                'reasoning': 'take the opposite side',
            })

    validated, raw_output, reject_reason = run_llm_decision(
        provider=StubProvider(),
        market_context={'market_id': 'm1', 'question': 'BTC?', 'window': '5m', 'venue': 'polymarket'},
        signal_data={'action': 'no', 'edge': 0.1, 'confidence': 0.7, 'signal_source': 'unit-test', 'window': '5m'},
        regime={'approved': True, 'reasons': []},
        risk_state={'allowed': True, 'reasons': [], 'trade_amount_usd': 4},
        live_params={'min_edge': 0.01, 'min_confidence': 0.58, 'max_slippage_pct': 0.1},
        pending_rules={'rules': []},
        learning_snapshot={'avg_edge': 0.1, 'avg_confidence': 0.7},
    )
    assert validated is None
    assert json.loads(raw_output)['action'] == 'yes'
    assert reject_reason == 'action must match signal.action=no or skip'


def test_run_llm_decision_retries_once_on_malformed_json():
    class StubProvider:
        provider_name = 'stub'
        model_name = 'stub-model'

        def __init__(self):
            self.calls = 0

        def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            del system_prompt, user_prompt
            self.calls += 1
            if self.calls == 1:
                return 'Here is the JSON requested'
            return json.dumps({
                'asset': 'BTC',
                'action': 'skip',
                'confidence': 0.7,
                'edge': 0.1,
                'reasoning': 'retry produced valid json',
            })

    provider = StubProvider()
    validated, raw_output, reject_reason = run_llm_decision(
        provider=provider,
        market_context={'market_id': 'm1', 'question': 'BTC?', 'window': '5m', 'venue': 'polymarket'},
        signal_data={'action': 'yes', 'edge': 0.1, 'confidence': 0.7, 'signal_source': 'unit-test', 'window': '5m'},
        regime={'approved': True, 'reasons': []},
        risk_state={'allowed': True, 'reasons': [], 'trade_amount_usd': 4},
        live_params={'min_edge': 0.01, 'min_confidence': 0.58, 'max_slippage_pct': 0.1},
        pending_rules={'rules': []},
        learning_snapshot={'avg_edge': 0.1, 'avg_confidence': 0.7},
    )
    assert provider.calls == 2
    assert reject_reason is None
    assert validated is not None
    assert validated['action'] == 'skip'
    assert json.loads(raw_output)['action'] == 'skip'
