from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ALLOWED_ACTIONS = {'yes', 'no', 'skip'}
LEARNABLE_KEYS = {
    'min_edge',
    'min_confidence',
    'max_slippage_pct',
    'cycle_interval_minutes',
    'stop_loss_pct',
    'take_profit_pct',
}
DEFAULT_OPENAI_MODEL = 'gpt-5-mini'
DEFAULT_OPENROUTER_MODEL = 'google/gemini-2.5-pro'
DEFAULT_GOOGLE_MODEL = 'gemini-2.5-flash'
DEFAULT_DEEPSEEK_MODEL = 'deepseek-chat'
DEFAULT_CODEX_MODEL = 'codex-mini-latest'
DEFAULT_OPENAI_BASE_URL = 'https://api.openai.com/v1'
DEFAULT_OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
DEFAULT_GOOGLE_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/openai/'
DEFAULT_DEEPSEEK_BASE_URL = 'https://api.deepseek.com'
CODEX_AUTH_PATH = Path.home() / '.codex' / 'auth.json'
MAX_MODEL_OUTPUT_TOKENS = 2048
LLM_REQUEST_TIMEOUT_SECONDS = 30
LLM_429_BACKOFF_SECONDS = (2, 5)
MAX_PROMPT_LIST_ITEMS = 1
MAX_USER_PROMPT_CHARS = 900
LLM_BLOCKER = 'missing LLM provider credentials'
STRICT_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'asset': {'type': 'string', 'enum': ['BTC']},
        'action': {'type': 'string', 'enum': ['yes', 'no', 'skip']},
        'confidence': {'type': 'number', 'minimum': 0.0, 'maximum': 1.0},
        'edge': {'type': 'number', 'minimum': 0.0, 'maximum': 1.0},
        'reasoning': {'type': 'string'},
        'rule_suggestion': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'key': {'type': 'string'},
                'value': {'type': 'string'},
                'why': {'type': 'string'},
            },
            'required': ['key', 'value', 'why'],
        },
    },
    'required': ['asset', 'action', 'confidence', 'edge', 'reasoning'],
}


class LLMError(RuntimeError):
    pass


class MissingLLMCredentialsError(LLMError):
    pass


class InvalidLLMOutputError(LLMError):
    pass


class ProviderRequestError(LLMError):
    pass


LLMProviderError = LLMError


class LLMProvider(Protocol):
    provider_name: str
    model_name: str

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class LLMDecision:
    asset: str
    action: str
    confidence: float
    edge: float
    reasoning: str
    rule_suggestion: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            'asset': self.asset,
            'action': self.action,
            'confidence': float(self.confidence),
            'edge': float(self.edge),
            'reasoning': self.reasoning,
        }
        if self.rule_suggestion is not None:
            data['rule_suggestion'] = self.rule_suggestion
        return data


@dataclass(frozen=True)
class LLMDecisionResult:
    ts: str
    provider: str | None
    model: str | None
    market_id: str
    window: str
    btc_only: bool
    raw_model_output: str | None
    raw_model_payload: dict[str, Any] | None
    validated_decision: dict[str, Any] | None
    reject_reason: str | None
    execution_status: str
    outcome: str | None
    signal_data: dict[str, Any]
    regime: dict[str, Any]
    risk_state: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            'ts': self.ts,
            'provider': self.provider,
            'model': self.model,
            'market_id': self.market_id,
            'window': self.window,
            'btc_only': self.btc_only,
            'raw_model_output': self.raw_model_output,
            'raw_model_payload': self.raw_model_payload,
            'validated_decision': self.validated_decision,
            'reject_reason': self.reject_reason,
            'execution_status': self.execution_status,
            'outcome': self.outcome,
            'signal_data': self.signal_data,
            'regime': self.regime,
            'risk_state': self.risk_state,
        }


def _request_model_name(provider_name: str, model_name: str) -> str:
    normalized = model_name.strip()
    if provider_name == 'google' and normalized.startswith('google/'):
        return normalized.removeprefix('google/')
    if provider_name == 'google_oauth' and normalized and not normalized.startswith('google/'):
        return f'google/{normalized}'
    return normalized


class OpenAIResponsesProvider:
    def __init__(self, *, provider_name: str, api_key: str, model_name: str, base_url: str | None = None) -> None:
        self.provider_name = provider_name
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_OPENAI_BASE_URL).rstrip('/')

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        try:
            return self._complete_chat(system_prompt=system_prompt, user_prompt=user_prompt, structured_output=True)
        except ProviderRequestError as exc:
            if not self._should_retry_without_schema(exc):
                raise
        return self._complete_chat(system_prompt=system_prompt, user_prompt=user_prompt, structured_output=False)

    def _complete_chat(self, *, system_prompt: str, user_prompt: str, structured_output: bool) -> str:
        body = {
            'model': _request_model_name(self.provider_name, self.model_name),
            'max_tokens': MAX_MODEL_OUTPUT_TOKENS,
            'messages': [
                {
                    'role': 'system',
                    'content': system_prompt,
                },
                {
                    'role': 'user',
                    'content': user_prompt,
                },
            ],
        }
        if structured_output:
            body['response_format'] = {
                'type': 'json_schema',
                'json_schema': {
                    'name': 'btc_trade_decision',
                    'schema': STRICT_SCHEMA,
                    'strict': True,
                },
            }

        for attempt_index, backoff_seconds in enumerate((0, *LLM_429_BACKOFF_SECONDS)):
            if backoff_seconds:
                time.sleep(backoff_seconds)
            try:
                raw = _post_json(
                    f'{self.base_url}/chat/completions',
                    body=body,
                    headers={
                        'Authorization': f'Bearer {self.api_key}',
                        'Content-Type': 'application/json',
                    },
                )
            except ProviderRequestError as exc:
                if 'provider request failed: 429' in str(exc) and attempt_index < len(LLM_429_BACKOFF_SECONDS):
                    continue
                raise
            content = _extract_response_text(raw)
            if content is None:
                raise ProviderRequestError(f'provider returned non-text content ({_summarize_response_shape(raw)})')
            if not content.strip():
                raise ProviderRequestError(f'provider returned empty text content ({_summarize_response_shape(raw)})')
            return content
        raise ProviderRequestError('provider returned non-text content (exhausted retries)')

    def _should_retry_without_schema(self, exc: ProviderRequestError) -> bool:
        message = str(exc).lower()
        non_json = 'provider returned non-text content' in message or 'provider returned empty text content' in message
        http_schema_error = 'provider request failed: 400' in message or 'provider request failed: 422' in message
        if not http_schema_error and not non_json:
            return False
        if self.provider_name in {'openrouter', 'deepseek', 'google', 'google_oauth'}:
            return True
        return any(marker in message for marker in ('response_format', 'json_schema', 'schema', 'strict', 'unsupported'))


class GCloudOAuthProvider:
    """LLM provider that authenticates with Google Cloud Application Default Credentials.

    Obtains a fresh OAuth2 access token via ``google-auth`` on every call so
    tokens are never stale.  Set up credentials on the host with::

        gcloud auth application-default login

    Then set ``LLM_PROVIDER=google_oauth`` (and optionally ``LLM_MODEL``) in
    the environment — no API key required.
    """

    provider_name = 'google_oauth'

    def __init__(
        self,
        *,
        model_name: str,
        base_url: str | None = None,
        project_id: str | None = None,
        location: str = 'global',
    ) -> None:
        self.model_name = model_name
        self.base_url = base_url.rstrip('/') if base_url else None
        self.project_id = (project_id or '').strip() or None
        self.location = (location or 'global').strip() or 'global'

    def _fresh_access_context(self) -> tuple[str, str]:
        try:
            import google.auth
            import google.auth.transport.requests
        except ImportError as exc:
            raise LLMError(
                'google-auth is required for google_oauth provider: pip install google-auth requests'
            ) from exc
        credentials, detected_project_id = google.auth.default(
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
        credentials.refresh(google.auth.transport.requests.Request())
        project_id = self.project_id or (detected_project_id or '').strip()
        if not project_id and not self.base_url:
            raise MissingLLMCredentialsError(
                'google_oauth provider: set GOOGLE_CLOUD_PROJECT or configure ADC with a default project'
            )
        base_url = self.base_url or (
            f'https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/{self.location}/endpoints/openapi'
        )
        return credentials.token, base_url

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        token, base_url = self._fresh_access_context()
        delegate = OpenAIResponsesProvider(
            provider_name='google_oauth',
            api_key=token,
            model_name=self.model_name,
            base_url=base_url,
        )
        return delegate.complete(system_prompt=system_prompt, user_prompt=user_prompt)


class CodexOAuthProvider:
    """LLM provider that uses the OpenAI Codex CLI OAuth token.

    Token resolution order:
    1. ``CODEX_OAUTH_TOKEN`` environment variable
    2. ``~/.codex/auth.json`` (written by ``codex login`` on the host)

    Set ``LLM_PROVIDER=codex`` — no separate API key needed.
    Model defaults to ``codex-mini-latest``; override with ``LLM_MODEL``.
    """

    provider_name = 'codex'

    def __init__(self, *, model_name: str, base_url: str | None = None) -> None:
        self.model_name = model_name
        self.base_url = (base_url or DEFAULT_OPENAI_BASE_URL).rstrip('/')

    @staticmethod
    def _resolve_token() -> str:
        token = (os.environ.get('CODEX_OAUTH_TOKEN') or '').strip()
        if token:
            return token
        if CODEX_AUTH_PATH.exists():
            try:
                data = json.loads(CODEX_AUTH_PATH.read_text())
                token = (
                    data.get('token')
                    or data.get('access_token')
                    or data.get('OPENAI_API_KEY')
                    or (data.get('tokens') or {}).get('access_token')
                    or (data.get('tokens') or {}).get('id_token')
                    or ''
                ).strip()
                if token:
                    return token
            except Exception:
                pass
        raise MissingLLMCredentialsError(
            'codex provider: set CODEX_OAUTH_TOKEN or run `codex login` on the host'
        )

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        token = self._resolve_token()
        delegate = OpenAIResponsesProvider(
            provider_name='openai',
            api_key=token,
            model_name=self.model_name,
            base_url=self.base_url,
        )
        return delegate.complete(system_prompt=system_prompt, user_prompt=user_prompt)


class StubOpenAIResponsesProvider:
    def __init__(self, *, provider_name: str, model_name: str, stub_response: str) -> None:
        self.provider_name = provider_name
        self.model_name = model_name
        self.stub_response = stub_response

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        return self.stub_response


def current_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + '\n')


def load_learned_params(path: Path) -> dict[str, Any]:
    payload = read_json_file(path, {})
    if not isinstance(payload, dict):
        return {}
    learned: dict[str, Any] = {}
    for key, value in payload.items():
        if key in LEARNABLE_KEYS and value is not None:
            learned[key] = value
    return learned


def load_live_params(path: Path) -> dict[str, Any]:
    return load_learned_params(path)


def load_pending_rules(path: Path) -> dict[str, Any]:
    payload = read_json_file(path, {'rules': []})
    if not isinstance(payload, dict):
        return {'rules': []}
    rules = payload.get('rules')
    if not isinstance(rules, list):
        payload['rules'] = []
    if '_meta' not in payload or not isinstance(payload.get('_meta'), dict):
        payload['_meta'] = {}
    return payload


def merge_learned_params(config: dict[str, Any], learned_params: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(config)
    for key in LEARNABLE_KEYS:
        if key in learned_params and learned_params[key] is not None:
            merged[key] = learned_params[key]
    return merged


def build_llm_context(
    *,
    config: Mapping[str, Any],
    market: Any,
    window: str,
    context: Mapping[str, Any],
    signal: Any,
    regime: Mapping[str, Any],
    settings: Mapping[str, Any],
    positions: list[Any],
) -> dict[str, Any]:
    return {
        'asset': config.get('asset', 'BTC'),
        'market_id': getattr(market, 'id', None),
        'market_question': _compact_text(getattr(market, 'question', ''), 120),
        'window': window,
        'signal': {
            key: value
            for key, value in (
                ('edge', getattr(signal, 'edge', None)),
                ('confidence', getattr(signal, 'confidence', None)),
                ('signal_source', getattr(signal, 'signal_source', None)),
                ('window', window),
            )
            if value is not None
        },
        'signal_action': getattr(signal, 'action', None),
        'regime': {
            'approved': regime.get('approved'),
            'reasons': _compact_list(regime.get('reasons'), 2),
            'warnings': _compact_list(regime.get('warnings'), 1),
        },
        'market_context': _summarize_market_context(context),
        'risk_limits': {
            key: config.get(key)
            for key in (
                'bankroll_usd',
                'max_trade_usd',
                'max_daily_loss_usd',
                'max_open_positions',
                'max_single_market_exposure_usd',
                'max_trades_per_hour',
                'max_slippage_pct',
                'stop_loss_pct',
                'take_profit_pct',
                'cooldown_after_loss_minutes',
            )
        },
        'account_state': {
            'positions_count': len(positions),
            'trading_venue': config.get('trading_venue'),
        },
        'execution_profile': config.get('execution_profile', 'balanced'),
        'live_params': {
            key: value
            for key, value in load_live_params(Path(config['live_params_path'])).items()
            if key in {'min_edge', 'min_confidence', 'max_slippage_pct', 'cycle_interval_minutes'}
        } if config.get('live_params_path') else {},
    }


def build_provider_from_env(env: Mapping[str, str] | None = None) -> LLMProvider:
    env = env or os.environ
    provider_name = (env.get('LLM_PROVIDER') or '').strip().lower()
    if not provider_name:
        if (env.get('GOOGLE_API_KEY') or '').strip() or (env.get('GEMINI_API_KEY') or '').strip():
            provider_name = 'google'
        elif (env.get('LLM_API_KEY') or '').strip() or (env.get('OPENAI_API_KEY') or '').strip() or (env.get('OPENROUTER_API_KEY') or '').strip():
            provider_name = 'openai'
        elif (env.get('DEEPSEEK_API_KEY') or '').strip():
            provider_name = 'deepseek'
        else:
            provider_name = 'openai'

    if provider_name not in {'openai', 'openrouter', 'deepseek', 'google', 'google_oauth', 'codex'}:
        raise MissingLLMCredentialsError(f'unsupported LLM provider: {provider_name}')

    # codex resolves its token from CODEX_OAUTH_TOKEN or ~/.codex/auth.json — return early.
    if provider_name == 'codex':
        model_name = (
            env.get('LLM_MODEL')
            or env.get('CODEX_MODEL')
            or DEFAULT_CODEX_MODEL
        ).strip()
        base_url = (env.get('LLM_BASE_URL') or env.get('OPENAI_BASE_URL') or DEFAULT_OPENAI_BASE_URL).strip() or None
        return CodexOAuthProvider(model_name=model_name, base_url=base_url)

    # google_oauth uses ADC — no API key needed, return early.
    if provider_name == 'google_oauth':
        model_name = (
            env.get('LLM_MODEL')
            or env.get('GOOGLE_MODEL')
            or env.get('GEMINI_MODEL')
            or DEFAULT_GOOGLE_MODEL
        ).strip()
        base_url = (
            env.get('LLM_BASE_URL')
            or env.get('GOOGLE_BASE_URL')
            or env.get('GEMINI_BASE_URL')
            or ''
        ).strip() or None
        project_id = (
            env.get('GOOGLE_CLOUD_PROJECT')
            or env.get('GCLOUD_PROJECT')
            or env.get('GOOGLE_PROJECT_ID')
            or ''
        ).strip() or None
        location = (
            env.get('GOOGLE_CLOUD_LOCATION')
            or env.get('VERTEX_LOCATION')
            or env.get('GOOGLE_LOCATION')
            or 'global'
        ).strip() or 'global'
        return GCloudOAuthProvider(
            model_name=model_name,
            base_url=base_url,
            project_id=project_id,
            location=location,
        )

    if provider_name == 'google':
        api_key = (
            (env.get('LLM_API_KEY') or '').strip()
            or (env.get('GOOGLE_API_KEY') or '').strip()
            or (env.get('GEMINI_API_KEY') or '').strip()
        )
    elif provider_name == 'deepseek':
        api_key = (
            (env.get('LLM_API_KEY') or '').strip()
            or (env.get('DEEPSEEK_API_KEY') or '').strip()
            or (env.get('OPENAI_API_KEY') or '').strip()
            or (env.get('OPENROUTER_API_KEY') or '').strip()
        )
    else:
        api_key = (
            (env.get('LLM_API_KEY') or '').strip()
            or (env.get('OPENAI_API_KEY') or '').strip()
            or (env.get('OPENROUTER_API_KEY') or '').strip()
        )
    if not api_key:
        raise MissingLLMCredentialsError(LLM_BLOCKER)

    if provider_name == 'deepseek':
        default_model = DEFAULT_DEEPSEEK_MODEL
    elif provider_name == 'google':
        default_model = DEFAULT_GOOGLE_MODEL
    elif provider_name == 'openrouter':
        default_model = DEFAULT_OPENROUTER_MODEL
    else:
        default_model = DEFAULT_OPENAI_MODEL
    if provider_name == 'google':
        model_name = (
            env.get('LLM_MODEL')
            or env.get('GOOGLE_MODEL')
            or env.get('GEMINI_MODEL')
            or default_model
        ).strip()
    elif provider_name == 'openrouter':
        model_name = (
            env.get('LLM_MODEL')
            or env.get('OPENROUTER_MODEL')
            or env.get('OPENAI_MODEL')
            or default_model
        ).strip()
    elif provider_name == 'deepseek':
        model_name = (
            env.get('LLM_MODEL')
            or env.get('DEEPSEEK_MODEL')
            or default_model
        ).strip()
    else:
        model_name = (
            env.get('LLM_MODEL')
            or env.get('OPENAI_MODEL')
            or default_model
        ).strip()
    if not model_name:
        raise MissingLLMCredentialsError(LLM_BLOCKER)

    stub_response = (env.get('OPENAI_STUB_RESPONSE') or '').strip()
    if stub_response:
        return StubOpenAIResponsesProvider(
            provider_name=provider_name,
            model_name=model_name,
            stub_response=stub_response,
        )

    if provider_name == 'deepseek':
        default_base_url = DEFAULT_DEEPSEEK_BASE_URL
    elif provider_name == 'google':
        default_base_url = DEFAULT_GOOGLE_BASE_URL
    elif provider_name == 'openrouter':
        default_base_url = DEFAULT_OPENROUTER_BASE_URL
    else:
        default_base_url = DEFAULT_OPENAI_BASE_URL
    if provider_name == 'google':
        base_url = (
            env.get('LLM_BASE_URL')
            or env.get('GOOGLE_BASE_URL')
            or env.get('GEMINI_BASE_URL')
            or default_base_url
        ).strip() or None
    elif provider_name == 'openrouter':
        base_url = (
            env.get('LLM_BASE_URL')
            or env.get('OPENROUTER_BASE_URL')
            or env.get('OPENAI_BASE_URL')
            or default_base_url
        ).strip() or None
    elif provider_name == 'deepseek':
        base_url = (
            env.get('LLM_BASE_URL')
            or env.get('DEEPSEEK_BASE_URL')
            or env.get('OPENAI_BASE_URL')
            or default_base_url
        ).strip() or None
    else:
        base_url = (
            env.get('LLM_BASE_URL')
            or env.get('OPENAI_BASE_URL')
            or default_base_url
        ).strip() or None
    return OpenAIResponsesProvider(
        provider_name=provider_name,
        api_key=api_key,
        model_name=model_name,
        base_url=base_url,
    )


def build_system_prompt() -> str:
    schema_json = json.dumps(STRICT_SCHEMA, separators=(',', ':'), sort_keys=True)
    return (
        'Return one BTC-only JSON object that validates this schema:\n'
        f'{schema_json}\n'
        'Your entire reply must be exactly one JSON object. '
        'Do not include markdown, code fences, labels, or any text before or after the JSON. '
        'The first character must be { and the last character must be }. '
        'Use yes, no, or skip. Keep reasoning concise. '
        'If signal.action is yes or no, return that same action or skip; never invert signal.action.'
    )


def build_repair_system_prompt() -> str:
    return (
        build_system_prompt()
        + ' Your previous reply was invalid. Reply again with only the corrected JSON object. '
        + 'If you are unsure, use action="skip".'
    )


def _compact_text(value: Any, limit: int = 160) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + '…'
    return value


def _compact_list(values: Any, limit: int = MAX_PROMPT_LIST_ITEMS) -> list[Any]:
    if not isinstance(values, list):
        return []
    return [_compact_text(item) for item in values[:limit]]


def _compact_rule_list(pending_rules: Mapping[str, Any]) -> dict[str, Any]:
    rules = pending_rules.get('rules') if isinstance(pending_rules, Mapping) else []
    compact_rules: list[dict[str, Any]] = []
    if isinstance(rules, list):
        for rule in rules[-MAX_PROMPT_LIST_ITEMS:]:
            if not isinstance(rule, Mapping):
                continue
            compact_rule = {
                'key': rule.get('key'),
                'value': _compact_text(rule.get('value')),
                'status': rule.get('status'),
                'count': rule.get('count'),
            }
            compact_rules.append({k: v for k, v in compact_rule.items() if v is not None})
    return {
        'count': len(rules) if isinstance(rules, list) else 0,
        'recent': compact_rules,
    }


def _summarize_market_context(market_context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'market_id': market_context.get('market_id'),
        'question': _compact_text(market_context.get('question'), 120),
        'window': market_context.get('window'),
        'venue': market_context.get('venue'),
        'fee_rate_bps': market_context.get('fee_rate_bps'),
        'spread_pct': market_context.get('spread_pct'),
    }


def _summarize_signal(signal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'action': signal.get('action'),
        'edge': signal.get('edge'),
        'confidence': signal.get('confidence'),
        'net_score': signal.get('net_score'),
        'reasoning': _compact_text(signal.get('reasoning'), 120),
        'signal_source': signal.get('signal_source'),
        'window': signal.get('window'),
    }


def _summarize_gate_state(regime: Mapping[str, Any], risk_state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'regime_approved': regime.get('approved'),
        'regime_reasons': _compact_list(regime.get('reasons'), 2),
        'risk_allowed': risk_state.get('allowed'),
        'risk_reasons': _compact_list(risk_state.get('reasons'), 2),
        'trade_amount_usd': risk_state.get('trade_amount_usd'),
    }


def _build_compact_prompt_payload(
    *,
    market_context: Mapping[str, Any],
    signal: Mapping[str, Any],
    regime: Mapping[str, Any],
    risk_state: Mapping[str, Any],
    live_params: Mapping[str, Any],
    learning_snapshot: Mapping[str, Any],
    question_limit: int,
    include_live_params: bool,
    include_learning_snapshot: bool,
) -> dict[str, Any]:
    payload = {
        'market': _summarize_market_context(market_context),
        'signal': _summarize_signal(signal),
        'gate': _summarize_gate_state(regime, risk_state),
        'required_output': {
            'asset': 'BTC',
            'action': 'yes|no|skip',
            'confidence': 'number 0..1',
            'edge': 'number 0..1',
            'reasoning': 'string',
            'rule_suggestion': {'key': 'optional', 'value': 'optional', 'why': 'optional'},
        },
    }
    payload['market']['question'] = _compact_text(market_context.get('question'), question_limit)
    if include_live_params:
        payload['live_params'] = {
            key: live_params.get(key)
            for key in ('min_edge', 'min_confidence', 'max_slippage_pct')
            if key in live_params
        }
    if include_learning_snapshot:
        payload['learning_snapshot'] = {
            'avg_edge': learning_snapshot.get('avg_edge'),
            'avg_confidence': learning_snapshot.get('avg_confidence'),
        }
    return payload


def build_compact_user_prompt(*, market_context: Mapping[str, Any], signal: Mapping[str, Any], regime: Mapping[str, Any], risk_state: Mapping[str, Any], live_params: Mapping[str, Any], pending_rules: Mapping[str, Any], learning_snapshot: Mapping[str, Any]) -> str:
    del pending_rules
    payload_variants = (
        _build_compact_prompt_payload(
            market_context=market_context,
            signal=signal,
            regime=regime,
            risk_state=risk_state,
            live_params=live_params,
            learning_snapshot=learning_snapshot,
            question_limit=120,
            include_live_params=True,
            include_learning_snapshot=True,
        ),
        _build_compact_prompt_payload(
            market_context=market_context,
            signal=signal,
            regime=regime,
            risk_state=risk_state,
            live_params=live_params,
            learning_snapshot=learning_snapshot,
            question_limit=80,
            include_live_params=True,
            include_learning_snapshot=False,
        ),
        _build_compact_prompt_payload(
            market_context=market_context,
            signal=signal,
            regime=regime,
            risk_state=risk_state,
            live_params=live_params,
            learning_snapshot=learning_snapshot,
            question_limit=48,
            include_live_params=False,
            include_learning_snapshot=False,
        ),
    )
    fallback_prompt = ''
    for payload in payload_variants:
        prompt = json.dumps(payload, separators=(',', ':'), sort_keys=True, default=str)
        fallback_prompt = prompt
        if len(prompt) <= MAX_USER_PROMPT_CHARS:
            return prompt
    return fallback_prompt


def build_user_prompt(*, market_context: Mapping[str, Any], signal: Mapping[str, Any], regime: Mapping[str, Any], risk_state: Mapping[str, Any], live_params: Mapping[str, Any], pending_rules: Mapping[str, Any], learning_snapshot: Mapping[str, Any]) -> str:
    return build_compact_user_prompt(
        market_context=market_context,
        signal=signal,
        regime=regime,
        risk_state=risk_state,
        live_params=live_params,
        pending_rules=pending_rules,
        learning_snapshot=learning_snapshot,
    )


def parse_model_output(raw_output: str) -> dict[str, Any]:
    stripped = raw_output.strip()
    if not stripped:
        raise InvalidLLMOutputError('malformed JSON: empty response')

    candidates: list[str] = [stripped]
    fenced_match = re.search(r'```(?:json)?\s*(.*?)\s*```', stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced_match:
        candidates.append(fenced_match.group(1).strip())
    first_brace = stripped.find('{')
    last_brace = stripped.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidates.append(stripped[first_brace:last_brace + 1].strip())

    json_error: json.JSONDecodeError | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
            break
        except json.JSONDecodeError as exc:
            json_error = exc
    else:
        if json_error is None:
            raise InvalidLLMOutputError('malformed JSON: empty response')
        raise InvalidLLMOutputError(f'malformed JSON: {json_error.msg}') from json_error
    if not isinstance(payload, dict):
        raise InvalidLLMOutputError('model output must be a JSON object')
    return payload


def _coerce_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise InvalidLLMOutputError(f'{field_name} must be numeric')
    if isinstance(value, (int, float)):
        return float(value)
    raise InvalidLLMOutputError(f'{field_name} must be numeric')


def validate_model_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise InvalidLLMOutputError('model output must be a JSON object')

    allowed_top_keys = {'asset', 'action', 'confidence', 'edge', 'reasoning', 'rule_suggestion'}
    unexpected = set(payload.keys()) - allowed_top_keys
    if unexpected:
        raise InvalidLLMOutputError(f'unexpected keys: {", ".join(sorted(unexpected))}')

    asset = payload.get('asset')
    if asset != 'BTC':
        raise InvalidLLMOutputError('asset must be BTC')

    action = payload.get('action')
    if action not in ALLOWED_ACTIONS:
        raise InvalidLLMOutputError('action must be yes, no, or skip')

    confidence = _coerce_number(payload.get('confidence'), 'confidence')
    edge = _coerce_number(payload.get('edge'), 'edge')
    if not 0.0 <= confidence <= 1.0:
        raise InvalidLLMOutputError('confidence must be between 0 and 1')
    if not 0.0 <= edge <= 1.0:
        raise InvalidLLMOutputError('edge must be between 0 and 1')

    reasoning = payload.get('reasoning')
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise InvalidLLMOutputError('reasoning must be a non-empty string')

    rule_suggestion = payload.get('rule_suggestion')
    validated_rule: dict[str, str] | None = None
    if rule_suggestion is not None:
        if not isinstance(rule_suggestion, Mapping):
            raise InvalidLLMOutputError('rule_suggestion must be an object')
        allowed_rule_keys = {'key', 'value', 'why'}
        unexpected_rule = set(rule_suggestion.keys()) - allowed_rule_keys
        if unexpected_rule:
            raise InvalidLLMOutputError(f'unexpected rule_suggestion keys: {", ".join(sorted(unexpected_rule))}')
        validated_rule = {}
        for key in allowed_rule_keys:
            value = rule_suggestion.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise InvalidLLMOutputError(f'rule_suggestion.{key} must be a string')
            if value.strip():
                validated_rule[key] = value.strip()
        if not validated_rule:
            validated_rule = None

    validated = {
        'asset': 'BTC',
        'action': action,
        'confidence': confidence,
        'edge': edge,
        'reasoning': reasoning.strip(),
    }
    if validated_rule is not None:
        validated['rule_suggestion'] = validated_rule
    return validated


def validate_action_alignment(validated: Mapping[str, Any], signal_data: Mapping[str, Any]) -> dict[str, Any]:
    signal_action = signal_data.get('action')
    model_action = validated.get('action')
    if signal_action in {'yes', 'no'} and model_action in {'yes', 'no'} and model_action != signal_action:
        raise InvalidLLMOutputError(f'action must match signal.action={signal_action} or skip')
    return dict(validated)


def build_decision_record(
    *,
    market_id: str,
    window: str,
    signal_data: Mapping[str, Any],
    regime: Mapping[str, Any],
    risk_state: Mapping[str, Any],
    provider: LLMProvider | None,
    raw_model_output: str | None,
    raw_model_payload: Mapping[str, Any] | None,
    validated_decision: Mapping[str, Any] | None,
    reject_reason: str | None,
    execution_status: str,
    outcome: str | None = None,
) -> dict[str, Any]:
    return LLMDecisionResult(
        ts=current_ts(),
        provider=getattr(provider, 'provider_name', None),
        model=getattr(provider, 'model_name', None),
        market_id=market_id,
        window=window,
        btc_only=True,
        raw_model_output=raw_model_output,
        raw_model_payload=dict(raw_model_payload) if raw_model_payload is not None else None,
        validated_decision=dict(validated_decision) if validated_decision is not None else None,
        reject_reason=reject_reason,
        execution_status=execution_status,
        outcome=outcome,
        signal_data=dict(signal_data),
        regime=dict(regime),
        risk_state=dict(risk_state),
    ).to_record()


def decide_trade(*, context: Mapping[str, Any], provider: LLMProvider | None = None) -> dict[str, Any]:
    provider = provider or build_provider_from_env()
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(
        market_context=context.get('market_context') or {},
        signal=context.get('signal') or {},
        regime=context.get('regime') or {},
        risk_state=context.get('risk_limits') or {},
        live_params=context.get('live_params') or {},
        pending_rules=context.get('pending_rules') or {},
        learning_snapshot=context.get('learning_snapshot') or {},
    )
    raw_output = provider.complete(system_prompt=system_prompt, user_prompt=user_prompt)
    try:
        payload = parse_model_output(raw_output)
        validated = validate_model_output(payload)
        validation = {
            'accepted': True,
            'reject_reason': None,
            'raw_response': raw_output,
            'raw_payload': payload,
            'decision': validated,
        }
    except InvalidLLMOutputError as exc:
        validation = {
            'accepted': False,
            'reject_reason': str(exc),
            'raw_response': raw_output,
            'raw_payload': None,
            'decision': None,
        }
    return {
        'provider': provider.provider_name,
        'model': provider.model_name,
        'raw_response': raw_output,
        'validation': validation,
        'decision': validation.get('decision'),
    }


def finalize_trade_decision(validation: Mapping[str, Any], regime: Mapping[str, Any], risk_state: Mapping[str, Any]) -> dict[str, Any]:
    if not validation.get('accepted'):
        return {
            'final_action': 'reject',
            'reason': f"llm_reject:{validation.get('reject_reason')}",
            'regime_approved': bool(regime.get('approved')),
            'risk_allowed': bool(risk_state.get('allowed')),
        }

    decision = validation.get('decision') or {}
    action = decision.get('action')
    if action == 'skip':
        return {
            'final_action': 'skip',
            'reason': 'model_skip',
            'regime_approved': bool(regime.get('approved')),
            'risk_allowed': bool(risk_state.get('allowed')),
        }
    if action == 'no':
        return {
            'final_action': 'reject',
            'reason': 'model_no',
            'regime_approved': bool(regime.get('approved')),
            'risk_allowed': bool(risk_state.get('allowed')),
        }
    if not regime.get('approved'):
        return {
            'final_action': 'reject',
            'reason': 'regime_reject',
            'regime_approved': False,
            'risk_allowed': bool(risk_state.get('allowed')),
        }
    if not risk_state.get('allowed'):
        return {
            'final_action': 'reject',
            'reason': f"risk_reject:{','.join(str(reason) for reason in (risk_state.get('reasons') or []))}",
            'regime_approved': True,
            'risk_allowed': False,
        }
    return {
        'final_action': 'execute',
        'reason': 'approved',
        'regime_approved': True,
        'risk_allowed': True,
    }


def run_llm_decision(
    *,
    provider: LLMProvider | None,
    market_context: Mapping[str, Any],
    signal_data: Mapping[str, Any],
    regime: Mapping[str, Any],
    risk_state: Mapping[str, Any],
    live_params: Mapping[str, Any],
    pending_rules: Mapping[str, Any],
    learning_snapshot: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if provider is None:
        return None, None, LLM_BLOCKER

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(
        market_context=market_context,
        signal=signal_data,
        regime=regime,
        risk_state=risk_state,
        live_params=live_params,
        pending_rules=pending_rules,
        learning_snapshot=learning_snapshot,
    )
    raw_output = provider.complete(system_prompt=system_prompt, user_prompt=user_prompt)
    try:
        payload = parse_model_output(raw_output)
        validated = validate_model_output(payload)
        validated = validate_action_alignment(validated, signal_data)
        return validated, raw_output, None
    except InvalidLLMOutputError as exc:
        if not str(exc).startswith('malformed JSON:'):
            return None, raw_output, str(exc)

    repair_output = provider.complete(
        system_prompt=build_repair_system_prompt(),
        user_prompt=(
            f'{user_prompt}\n\n'
            'Your previous reply was invalid and must be corrected.\n'
            f'Invalid reply:\n{raw_output}'
        ),
    )
    try:
        payload = parse_model_output(repair_output)
        validated = validate_model_output(payload)
        validated = validate_action_alignment(validated, signal_data)
        return validated, repair_output, None
    except InvalidLLMOutputError as exc:
        return None, repair_output, str(exc)


def summarize_pending_rules(pending_rules: Mapping[str, Any]) -> dict[str, Any]:
    rules = pending_rules.get('rules') if isinstance(pending_rules, Mapping) else []
    rules = rules if isinstance(rules, list) else []
    return {
        'count': len(rules),
        'rules': rules,
    }


def record_pending_rule(
    *,
    path: Path,
    suggestion: Mapping[str, Any] | None,
    outcome: Any = None,
) -> dict[str, Any]:
    store = read_json_file(path, {'rules': []})
    if not isinstance(store, dict):
        store = {'rules': []}
    rules = store.get('rules')
    if not isinstance(rules, list):
        rules = []

    if suggestion:
        key = suggestion.get('key')
        value = suggestion.get('value')
        why = suggestion.get('why')
        if key in LEARNABLE_KEYS and value is not None:
            matched = None
            for entry in rules:
                if (
                    isinstance(entry, dict)
                    and entry.get('key') == key
                    and entry.get('value') == value
                    and entry.get('why') == why
                ):
                    matched = entry
                    break
            if matched is None:
                matched = {
                    'key': key,
                    'value': value,
                    'why': why,
                    'count': 0,
                    'accepted_count': 0,
                    'rejected_count': 0,
                    'status': 'pending',
                    'first_seen': current_ts(),
                    'last_seen': current_ts(),
                    'applied_at': None,
                    'rolled_back_at': None,
                }
                rules.append(matched)
            matched['count'] = int(matched.get('count') or 0) + 1
            matched['last_seen'] = current_ts()
            if isinstance(outcome, (int, float)) and outcome > 0:
                matched['accepted_count'] = int(matched.get('accepted_count') or 0) + 1
            elif isinstance(outcome, (int, float)) and outcome < 0:
                matched['rejected_count'] = int(matched.get('rejected_count') or 0) + 1

    store['rules'] = rules
    store['_meta'] = {
        'updated_at': current_ts(),
        'source': 'btc_llm_decider',
    }
    write_json_file(path, store)
    return store


def update_learning_state(
    *,
    live_params_path: Path,
    pending_rules_path: Path,
    defaults: Mapping[str, Any],
    cycle_entry: Mapping[str, Any],
    history: list[Mapping[str, Any]],
) -> dict[str, Any]:
    validation = cycle_entry.get('validation') or {}
    decision = validation.get('decision') or {}
    suggestion = decision.get('rule_suggestion')
    outcome = cycle_entry.get('outcome')

    record_pending_rule(
        path=pending_rules_path,
        suggestion=suggestion if isinstance(suggestion, Mapping) else None,
        outcome=outcome,
    )

    learning_snapshot = {
        'avg_edge': None,
        'avg_confidence': None,
    }
    accepted_rows = [
        row
        for row in history
        if isinstance(row, Mapping)
        and (row.get('validation') or {}).get('accepted')
        and ((row.get('validation') or {}).get('decision') or {}).get('action') == 'yes'
    ]
    edges = [
        float(((row.get('validation') or {}).get('decision') or {}).get('edge'))
        for row in accepted_rows
        if isinstance((((row.get('validation') or {}).get('decision') or {}).get('edge')), (int, float))
    ]
    confidences = [
        float(((row.get('validation') or {}).get('decision') or {}).get('confidence'))
        for row in accepted_rows
        if isinstance((((row.get('validation') or {}).get('decision') or {}).get('confidence')), (int, float))
    ]
    if edges:
        learning_snapshot['avg_edge'] = sum(edges) / len(edges)
    if confidences:
        learning_snapshot['avg_confidence'] = sum(confidences) / len(confidences)

    apply_eligible_rules(
        live_params_path=live_params_path,
        pending_rules_path=pending_rules_path,
        learning_snapshot=learning_snapshot,
        defaults=defaults,
    )
    return {
        'learning_snapshot': learning_snapshot,
        'pending_rules': load_pending_rules(pending_rules_path),
        'live_params': load_live_params(live_params_path),
    }


def apply_eligible_rules(
    *,
    live_params_path: Path,
    pending_rules_path: Path,
    learning_snapshot: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    live_params = read_json_file(live_params_path, {})
    if not isinstance(live_params, dict):
        live_params = {}
    pending_store = read_json_file(pending_rules_path, {'rules': []})
    if not isinstance(pending_store, dict):
        pending_store = {'rules': []}
    rules = pending_store.get('rules')
    if not isinstance(rules, list):
        rules = []

    avg_edge = learning_snapshot.get('avg_edge')
    avg_confidence = learning_snapshot.get('avg_confidence')
    for entry in rules:
        if not isinstance(entry, dict):
            continue
        if entry.get('status') != 'pending':
            continue
        if entry.get('count', 0) < 3:
            continue
        if entry.get('accepted_count', 0) < 2:
            continue
        key = entry.get('key')
        if key not in LEARNABLE_KEYS:
            continue

        template_value = defaults.get(key, live_params.get(key))
        raw_value = entry.get('value')
        new_value = coerce_rule_value(key, raw_value, template_value)
        baseline_edge = avg_edge if isinstance(avg_edge, (int, float)) else None
        baseline_confidence = avg_confidence if isinstance(avg_confidence, (int, float)) else None
        entry['previous_value'] = live_params.get(key)
        entry['applied_at'] = current_ts()
        entry['baseline_edge'] = baseline_edge
        entry['baseline_confidence'] = baseline_confidence
        entry['status'] = 'applied'
        live_params[key] = new_value

    if avg_edge is not None and avg_confidence is not None:
        for entry in rules:
            if not isinstance(entry, dict):
                continue
            if entry.get('status') != 'applied':
                continue
            baseline_edge = entry.get('baseline_edge')
            baseline_confidence = entry.get('baseline_confidence')
            if baseline_edge is None or baseline_confidence is None:
                continue
            if avg_edge < baseline_edge or avg_confidence < baseline_confidence:
                key = entry.get('key')
                previous_value = entry.get('previous_value')
                if key in LEARNABLE_KEYS and previous_value is not None:
                    live_params[key] = previous_value
                entry['status'] = 'rolled_back'
                entry['rolled_back_at'] = current_ts()

    live_params['_meta'] = {
        'updated_at': current_ts(),
        'source': 'btc_llm_decider',
    }
    pending_store['rules'] = rules
    pending_store['_meta'] = {
        'updated_at': current_ts(),
        'source': 'btc_llm_decider',
    }
    write_json_file(live_params_path, live_params)
    write_json_file(pending_rules_path, pending_store)
    return live_params, pending_store


def coerce_rule_value(key: str, raw_value: Any, template_value: Any) -> Any:
    if raw_value is None:
        return template_value
    if isinstance(template_value, bool):
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, str):
            return raw_value.strip().lower() in {'1', 'true', 'yes', 'on'}
        return bool(raw_value)
    if isinstance(template_value, int) and not isinstance(template_value, bool):
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            return int(raw_value)
        return int(float(str(raw_value)))
    if isinstance(template_value, float):
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            return float(raw_value)
        return float(str(raw_value))
    return raw_value


def _coerce_text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ('text', 'value', 'content'):
            nested = _coerce_text_value(value.get(key))
            if nested is not None:
                return nested
    return None


def _iter_response_text_candidates(raw: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    for choice in raw.get('choices') or []:
        if not isinstance(choice, Mapping):
            continue
        legacy_text = _coerce_text_value(choice.get('text'))
        if legacy_text is not None:
            candidates.append(legacy_text)
        message = choice.get('message')
        if not isinstance(message, Mapping):
            continue
        content = message.get('content')
        if isinstance(content, list):
            for block in content:
                block_text = _coerce_text_value(block)
                if block_text is not None:
                    candidates.append(block_text)
        else:
            message_text = _coerce_text_value(content)
            if message_text is not None:
                candidates.append(message_text)
    output_text = _coerce_text_value(raw.get('output_text'))
    if output_text is not None:
        candidates.append(output_text)
    outputs = raw.get('output')
    if isinstance(outputs, list):
        for item in outputs:
            if not isinstance(item, Mapping):
                continue
            content = item.get('content')
            if isinstance(content, list):
                for block in content:
                    block_text = _coerce_text_value(block)
                    if block_text is not None:
                        candidates.append(block_text)
            else:
                output_content_text = _coerce_text_value(content)
                if output_content_text is not None:
                    candidates.append(output_content_text)
    return candidates


def _extract_response_text(raw: Mapping[str, Any]) -> str | None:
    candidates = _iter_response_text_candidates(raw)
    for candidate in candidates:
        if not candidate.strip():
            continue
        try:
            parse_model_output(candidate)
            return candidate
        except InvalidLLMOutputError:
            continue
    return None


def _summarize_response_shape(raw: Mapping[str, Any]) -> str:
    bits: list[str] = [f"top_keys={','.join(sorted(str(key) for key in raw.keys())[:6])}"]
    choices = raw.get('choices')
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        if isinstance(first_choice, Mapping):
            bits.append(f"choice_keys={','.join(sorted(str(key) for key in first_choice.keys())[:6])}")
            message = first_choice.get('message')
            if isinstance(message, Mapping):
                bits.append(f"message_keys={','.join(sorted(str(key) for key in message.keys())[:6])}")
                bits.append(f"message_content_type={type(message.get('content')).__name__}")
    output = raw.get('output')
    if isinstance(output, list) and output:
        first_output = output[0]
        if isinstance(first_output, Mapping):
            bits.append(f"output_keys={','.join(sorted(str(key) for key in first_output.keys())[:6])}")
            bits.append(f"output_content_type={type(first_output.get('content')).__name__}")
    return '; '.join(bits)


def _post_json(url: str, *, body: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
    data = json.dumps(body).encode('utf-8')
    request_obj = Request(url, data=data, headers=dict(headers), method='POST')
    try:
        with urlopen(request_obj, timeout=LLM_REQUEST_TIMEOUT_SECONDS) as response:
            payload = response.read().decode('utf-8')
    except HTTPError as exc:
        error_body = exc.read().decode('utf-8', errors='replace') if exc.fp else ''
        raise ProviderRequestError(f'provider request failed: {exc.code} {error_body}'.strip()) from exc
    except URLError as exc:
        raise ProviderRequestError(f'provider request failed: {exc.reason}') from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProviderRequestError(f'provider returned invalid JSON: {exc.msg}') from exc
