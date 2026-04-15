from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.clob_auth_validator import (
    AuthValidationResult,
    ClobAuthConfig,
    ClobAuthValidator,
    SessionVerificationResult,
    SessionVerifier,
)
from engine.position_sizer import PositionSizer, PositionSizerConfig, SizingBalanceSnapshot
from execution.readiness_gate import ReadinessGateConfig, ReadinessGateResult, evaluate_readiness
from execution.trade_executor import DecisionRecord


DEFAULTS_PATH = ROOT / 'config' / 'defaults.json'
REQUIRED_FIELDS = (
    'requested_mode',
    'live_trading_enabled',
    'market_id',
    'market_observed_at',
    'balance_available_usdc',
    'balance_fetched_at',
    'health_state',
    'action',
    'target_notional_usd',
    'edge',
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> str:
    normalized = value.replace('Z', '+00:00')
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _parse_float(value: Any, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'invalid_float:{field_name}') from exc


def _parse_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f'invalid_boolean:{field_name}')


@dataclass(frozen=True)
class ProbeConfig:
    requested_mode: str
    live_trading_enabled: bool
    market_id: str
    market_observed_at: str
    balance_available_usdc: float
    balance_fetched_at: str
    health_state: str
    action: str
    target_notional_usd: float
    edge: float
    min_size: float = 1.0
    max_size: float = 4.0
    max_balance_fraction: float = 0.25
    min_edge: float = 0.07
    max_balance_age_seconds: float = 30.0
    max_market_age_seconds: float = 30.0


@dataclass(frozen=True)
class ProbeEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    status: str
    reasons: tuple[str, ...]
    auth_result: AuthValidationResult
    readiness_result: ReadinessGateResult
    events: tuple[ProbeEvent, ...]


class EnvironmentSessionVerifier:
    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def verify_session(self, _config: ClobAuthConfig) -> SessionVerificationResult:
        value = str(self._env.get('CLOB_SESSION_VERIFIED', '')).strip().lower()
        if value in {'1', 'true', 'yes', 'on'}:
            return SessionVerificationResult(verified=True)
        return SessionVerificationResult(verified=False, reason='session_not_verified')


def _emit(events: list[ProbeEvent], event_type: str, **details: Any) -> None:
    events.append(
        ProbeEvent(
            event_type=event_type,
            timestamp=_utc_now().isoformat(),
            details=details,
        )
    )


def _load_defaults(path: Path = DEFAULTS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_probe_config(path: Path, *, defaults_path: Path = DEFAULTS_PATH) -> ProbeConfig:
    payload = json.loads(path.read_text())
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ValueError(','.join(f'missing_config_field:{field}' for field in missing))

    defaults = _load_defaults(defaults_path)
    return ProbeConfig(
        requested_mode=str(payload['requested_mode']),
        live_trading_enabled=_parse_bool(payload['live_trading_enabled'], field_name='live_trading_enabled'),
        market_id=str(payload['market_id']),
        market_observed_at=_parse_timestamp(str(payload['market_observed_at'])),
        balance_available_usdc=_parse_float(payload['balance_available_usdc'], field_name='balance_available_usdc'),
        balance_fetched_at=_parse_timestamp(str(payload['balance_fetched_at'])),
        health_state=str(payload['health_state']),
        action=str(payload['action']),
        target_notional_usd=_parse_float(payload['target_notional_usd'], field_name='target_notional_usd'),
        edge=_parse_float(payload['edge'], field_name='edge'),
        min_size=_parse_float(payload.get('min_size', 1.0), field_name='min_size'),
        max_size=_parse_float(payload.get('max_size', defaults.get('max_trade_usd', 4.0)), field_name='max_size'),
        max_balance_fraction=_parse_float(payload.get('max_balance_fraction', 0.25), field_name='max_balance_fraction'),
        min_edge=_parse_float(payload.get('min_edge', defaults.get('min_edge', 0.07)), field_name='min_edge'),
        max_balance_age_seconds=_parse_float(
            payload.get('max_balance_age_seconds', 30.0),
            field_name='max_balance_age_seconds',
        ),
        max_market_age_seconds=_parse_float(
            payload.get('max_market_age_seconds', 30.0),
            field_name='max_market_age_seconds',
        ),
    )


def _build_auth_config(env: Mapping[str, str]) -> ClobAuthConfig:
    def _optional_int(key: str, default: int) -> int:
        raw = str(env.get(key, '')).strip()
        if not raw:
            return default
        return int(raw)

    return ClobAuthConfig(
        api_key=env.get('CLOB_API_KEY'),
        api_secret=env.get('CLOB_API_SECRET'),
        api_passphrase=env.get('CLOB_API_PASSPHRASE'),
        signer_address=env.get('CLOB_SIGNER_ADDRESS'),
        signer_key=env.get('CLOB_SIGNER_KEY'),
        chain_id=_optional_int('CLOB_CHAIN_ID', 137),
        signature_type=_optional_int('CLOB_SIGNATURE_TYPE', 1),
    )


def _build_decision(config: ProbeConfig) -> DecisionRecord:
    return DecisionRecord(
        decision_id=f'probe:{config.market_id}',
        round_id=f'probe:{config.market_id}',
        market_id=config.market_id,
        action=config.action,
        final_action=config.action,
        amount=config.target_notional_usd,
        edge=config.edge,
        gate_result={'allowed': True, 'reasons': []},
        feature_summary={
            'observed_at': config.market_observed_at,
            'available_balance_usdc': config.balance_available_usdc,
            'balance_fetched_at': config.balance_fetched_at,
            'health_state': config.health_state,
        },
        signal_data={},
    )


def _write_event_log(events: list[ProbeEvent], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in events:
            handle.write(json.dumps(asdict(event), sort_keys=True) + '\n')


def run_live_ready_probe(
    config_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    session_verifier: SessionVerifier | None = None,
    defaults_path: Path = DEFAULTS_PATH,
    event_log_path: Path | None = None,
) -> ProbeResult:
    probe_events: list[ProbeEvent] = []
    active_env = dict(os.environ) if env is None else dict(env)

    if not config_path.exists():
        auth_result = AuthValidationResult(status='auth_blocked', reasons=('missing_config_path',))
        readiness = ReadinessGateResult(status='blocked', reasons=('missing_config_path',))
        _emit(probe_events, 'probe_failed', reasons=['missing_config_path'])
        if event_log_path is not None:
            _write_event_log(probe_events, event_log_path)
        return ProbeResult(
            status='blocked',
            reasons=('missing_config_path',),
            auth_result=auth_result,
            readiness_result=readiness,
            events=tuple(probe_events),
        )

    try:
        config = load_probe_config(config_path, defaults_path=defaults_path)
    except json.JSONDecodeError:
        auth_result = AuthValidationResult(status='auth_blocked', reasons=('invalid_probe_config_json',))
        readiness = ReadinessGateResult(status='blocked', reasons=('invalid_probe_config_json',))
        _emit(probe_events, 'probe_failed', reasons=['invalid_probe_config_json'])
        if event_log_path is not None:
            _write_event_log(probe_events, event_log_path)
        return ProbeResult(
            status='blocked',
            reasons=('invalid_probe_config_json',),
            auth_result=auth_result,
            readiness_result=readiness,
            events=tuple(probe_events),
        )
    except ValueError as exc:
        reasons = tuple(str(exc).split(','))
        auth_result = AuthValidationResult(status='auth_blocked', reasons=reasons)
        readiness = ReadinessGateResult(status='blocked', reasons=reasons)
        _emit(probe_events, 'probe_failed', reasons=list(reasons))
        if event_log_path is not None:
            _write_event_log(probe_events, event_log_path)
        return ProbeResult(
            status='blocked',
            reasons=reasons,
            auth_result=auth_result,
            readiness_result=readiness,
            events=tuple(probe_events),
        )

    verifier = session_verifier or EnvironmentSessionVerifier(active_env)
    auth_validator = ClobAuthValidator(session_verifier=verifier)
    auth_result = auth_validator.validate(_build_auth_config(active_env))
    _emit(probe_events, 'auth_evaluated', status=auth_result.status, reasons=list(auth_result.reasons))

    decision = _build_decision(config)
    balance_snapshot = SizingBalanceSnapshot(
        available_usdc=config.balance_available_usdc,
        fetched_at=config.balance_fetched_at,
    )
    sizing = PositionSizer(
        config=PositionSizerConfig(
            min_size=config.min_size,
            max_size=config.max_size,
            max_balance_fraction=config.max_balance_fraction,
            max_balance_age_seconds=config.max_balance_age_seconds,
            min_edge=config.min_edge,
        )
    ).size(
        decision=decision,
        balance_snapshot=balance_snapshot,
    )
    _emit(
        probe_events,
        'sizing_evaluated',
        allowed=sizing.allowed,
        size=sizing.size,
        proposed_size=sizing.proposed_size,
        reasons=list(sizing.reasons),
    )

    readiness = evaluate_readiness(
        decision=decision,
        sizing=sizing,
        balance_snapshot=balance_snapshot,
        auth_result=auth_result,
        requested_mode=config.requested_mode,
        live_trading_enabled=config.live_trading_enabled,
        config=ReadinessGateConfig(
            min_order_size=config.min_size,
            max_balance_age_seconds=config.max_balance_age_seconds,
            max_market_age_seconds=config.max_market_age_seconds,
            required_health_state='ok',
        ),
    )
    _emit(probe_events, 'readiness_evaluated', status=readiness.status, reasons=list(readiness.reasons))
    _emit(probe_events, 'probe_completed', status=readiness.status, reasons=list(readiness.reasons))

    if event_log_path is not None:
        _write_event_log(probe_events, event_log_path)

    return ProbeResult(
        status=readiness.status,
        reasons=readiness.reasons,
        auth_result=auth_result,
        readiness_result=readiness,
        events=tuple(probe_events),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run a no-submit live readiness probe')
    parser.add_argument('--config', required=True, type=Path, help='Path to the normalized live probe config JSON')
    parser.add_argument('--event-log', type=Path, help='Optional JSONL path for append-only probe events')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_live_ready_probe(args.config, event_log_path=args.event_log)
    print(result.status)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
