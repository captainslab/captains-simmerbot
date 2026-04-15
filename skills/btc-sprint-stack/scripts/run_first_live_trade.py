from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

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
from adapters.polymarket_clob import BalanceSnapshot, PolymarketClobAdapter
from engine.decision_handoff import DecisionHandoff
from engine.feature_snapshot_builder import FeatureSnapshotInput, build_feature_snapshot
from engine.position_sizer import PositionSizer, PositionSizerConfig, SizingBalanceSnapshot
from engine.risk_gate import RiskGateConfig
from engine.vote_engine import VoteEngine, VoteEngineConfig
from execution.live_broker import LiveBroker, LiveBrokerConfig
from execution.readiness_gate import ReadinessGateConfig, ReadinessGateResult, evaluate_readiness
from execution.trade_executor import TradeExecutor
from replay.runner import ReplayRunner


DEFAULTS_PATH = ROOT / 'config' / 'defaults.json'
REQUIRED_FIELDS = (
    'requested_mode',
    'live_trading_enabled',
    'market_id',
    'market_observed_at',
    'health_state',
    'momentum',
    'market_price',
    'reference_price',
    'yes_pressure',
    'no_pressure',
    'requested_notional_usd',
    'max_first_trade_notional_usd',
)


class BrokerAdapter(Protocol):
    def fetch_balance(self) -> BalanceSnapshot: ...

    def place_order(self, **kwargs): ...


@dataclass(frozen=True)
class FirstLiveTradeConfig:
    requested_mode: str
    live_trading_enabled: bool
    market_id: str
    market_observed_at: str
    health_state: str
    momentum: float
    market_price: float
    reference_price: float
    yes_pressure: float
    no_pressure: float
    requested_notional_usd: float
    max_first_trade_notional_usd: float
    min_size: float = 1.0
    max_size: float = 4.0
    max_balance_fraction: float = 0.25
    min_edge: float = 0.07
    max_balance_age_seconds: float = 30.0
    max_market_age_seconds: float = 30.0


@dataclass(frozen=True)
class FirstLiveTradeEvent:
    event_type: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FirstLiveTradeResult:
    status: str
    reasons: tuple[str, ...]
    submit_attempted: bool
    execution_outcome: str
    events: tuple[FirstLiveTradeEvent, ...]


class EnvironmentSessionVerifier:
    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def verify_session(self, _config: ClobAuthConfig) -> SessionVerificationResult:
        value = str(self._env.get('CLOB_SESSION_VERIFIED', '')).strip().lower()
        if value in {'1', 'true', 'yes', 'on'}:
            return SessionVerificationResult(verified=True)
        return SessionVerificationResult(verified=False, reason='session_not_verified')


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _emit(events: list[FirstLiveTradeEvent], event_type: str, **details: Any) -> None:
    events.append(
        FirstLiveTradeEvent(
            event_type=event_type,
            timestamp=_utc_now().isoformat(),
            details=details,
        )
    )


def _write_event_log(events: list[FirstLiveTradeEvent], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in events:
            handle.write(json.dumps(asdict(event), sort_keys=True) + '\n')


def _parse_timestamp(value: str, *, field_name: str) -> str:
    normalized = value.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f'invalid_timestamp:{field_name}') from exc
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


def _load_defaults(path: Path = DEFAULTS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_first_live_trade_config(path: Path, *, defaults_path: Path = DEFAULTS_PATH) -> FirstLiveTradeConfig:
    payload = json.loads(path.read_text())
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ValueError(','.join(f'missing_config_field:{field}' for field in missing))

    defaults = _load_defaults(defaults_path)
    return FirstLiveTradeConfig(
        requested_mode=str(payload['requested_mode']),
        live_trading_enabled=_parse_bool(payload['live_trading_enabled'], field_name='live_trading_enabled'),
        market_id=str(payload['market_id']),
        market_observed_at=_parse_timestamp(str(payload['market_observed_at']), field_name='market_observed_at'),
        health_state=str(payload['health_state']),
        momentum=_parse_float(payload['momentum'], field_name='momentum'),
        market_price=_parse_float(payload['market_price'], field_name='market_price'),
        reference_price=_parse_float(payload['reference_price'], field_name='reference_price'),
        yes_pressure=_parse_float(payload['yes_pressure'], field_name='yes_pressure'),
        no_pressure=_parse_float(payload['no_pressure'], field_name='no_pressure'),
        requested_notional_usd=_parse_float(payload['requested_notional_usd'], field_name='requested_notional_usd'),
        max_first_trade_notional_usd=_parse_float(
            payload['max_first_trade_notional_usd'],
            field_name='max_first_trade_notional_usd',
        ),
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


def _build_live_adapter(env: Mapping[str, str]) -> BrokerAdapter:
    api_key = str(env.get('SIMMER_API_KEY', '')).strip()
    if not api_key:
        raise ValueError('missing_simmer_api_key')
    from simmer_sdk import SimmerClient

    client = SimmerClient(api_key=api_key, venue='polymarket', live=True)
    return PolymarketClobAdapter(client)


class FirstLiveTradeRunner:
    def __init__(
        self,
        config_path: Path,
        *,
        env: Mapping[str, str] | None = None,
        session_verifier: SessionVerifier | None = None,
        adapter: BrokerAdapter | None = None,
        event_log_path: Path | None = None,
        defaults_path: Path = DEFAULTS_PATH,
    ) -> None:
        self._config_path = config_path
        self._env = dict(os.environ) if env is None else dict(env)
        self._session_verifier = session_verifier
        self._adapter = adapter
        self._event_log_path = event_log_path
        self._defaults_path = defaults_path
        self._attempted = False

    def run(self) -> FirstLiveTradeResult:
        events: list[FirstLiveTradeEvent] = []
        if self._attempted:
            _emit(events, 'probe_result', status='blocked', reasons=['trade_already_attempted'])
            _emit(events, 'terminal_outcome', outcome='no_submit', reasons=['trade_already_attempted'])
            self._write_events(events)
            return FirstLiveTradeResult(
                status='blocked',
                reasons=('trade_already_attempted',),
                submit_attempted=False,
                execution_outcome='no_submit',
                events=tuple(events),
            )
        self._attempted = True

        try:
            config = load_first_live_trade_config(self._config_path, defaults_path=self._defaults_path)
        except FileNotFoundError:
            return self._blocked_result(events, 'missing_config_path')
        except json.JSONDecodeError:
            return self._blocked_result(events, 'invalid_first_live_trade_config_json')
        except ValueError as exc:
            reasons = tuple(str(exc).split(','))
            return self._blocked_result(events, *reasons)

        if config.max_first_trade_notional_usd <= 0:
            return self._blocked_result(events, 'invalid_first_trade_cap')

        try:
            adapter = self._adapter or _build_live_adapter(self._env)
        except ValueError as exc:
            return self._blocked_result(events, str(exc))

        auth_validator = ClobAuthValidator(
            session_verifier=self._session_verifier or EnvironmentSessionVerifier(self._env),
        )
        auth_result = auth_validator.validate(_build_auth_config(self._env))

        try:
            balance = adapter.fetch_balance()
        except Exception as exc:
            return self._blocked_result(events, f'balance_fetch_failed:{exc}')

        capped_notional = min(config.requested_notional_usd, config.max_first_trade_notional_usd)
        snapshot = build_feature_snapshot(
            FeatureSnapshotInput(
                market_id=config.market_id,
                observed_at=config.market_observed_at,
                market_price=config.market_price,
                reference_price=config.reference_price,
                momentum=config.momentum,
                yes_pressure=config.yes_pressure,
                no_pressure=config.no_pressure,
                available_balance_usdc=balance.available_usdc,
                health_state=config.health_state,
            ),
            now=_utc_now(),
        )
        handoff = DecisionHandoff(
            vote_engine=VoteEngine(
                config=VoteEngineConfig(
                    risk_gate=RiskGateConfig(
                        min_edge=config.min_edge,
                        max_snapshot_age_seconds=config.max_market_age_seconds,
                        min_balance_usdc=config.min_size,
                        required_health_state='ok',
                    )
                )
            )
        )
        round_id = f'first-live:{config.market_id}'
        decision = handoff.create_decision(
            round_id=round_id,
            snapshot=snapshot,
            trade_amount_usd=capped_notional,
        )
        balance_snapshot = SizingBalanceSnapshot(
            available_usdc=balance.available_usdc,
            fetched_at=balance.fetched_at,
        )
        position_sizer_config = PositionSizerConfig(
            min_size=config.min_size,
            max_size=min(config.max_size, config.max_first_trade_notional_usd),
            max_balance_fraction=config.max_balance_fraction,
            max_balance_age_seconds=config.max_balance_age_seconds,
            min_edge=config.min_edge,
        )
        sizing = PositionSizer(config=position_sizer_config).size(
            decision=decision,
            balance_snapshot=balance_snapshot,
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

        _emit(
            events,
            'probe_result',
            status=readiness.status,
            reasons=list(readiness.reasons),
            auth_status=auth_result.status,
            auth_reasons=list(auth_result.reasons),
            requested_notional=config.requested_notional_usd,
            capped_notional=capped_notional,
        )
        _emit(
            events,
            'decision_recorded',
            round_id=decision.round_id,
            action=decision.action,
            edge=decision.edge,
            reasoning=decision.reasoning,
        )
        _emit(
            events,
            'sizing_recorded',
            allowed=sizing.allowed,
            size=sizing.size,
            proposed_size=sizing.proposed_size,
            notional=sizing.notional,
            reasons=list(sizing.reasons),
        )

        broker = LiveBroker(
            adapter=adapter,
            config=LiveBrokerConfig(
                mode=config.requested_mode,
                live_trading_enabled=config.live_trading_enabled,
                auth_validation_result=auth_result,
            ),
        )
        runner = ReplayRunner(
            trade_executor=TradeExecutor(
                broker=broker,
                position_sizer_config=position_sizer_config,
                readiness_gate_config=ReadinessGateConfig(
                    min_order_size=config.min_size,
                    max_balance_age_seconds=config.max_balance_age_seconds,
                    max_market_age_seconds=config.max_market_age_seconds,
                    required_health_state='ok',
                ),
            )
        )
        round_result = runner.run_round(decision)
        execution = round_result.execution
        if execution is None:
            return self._blocked_result(events, 'missing_execution_result')

        submit_attempted = False
        for event in execution.events:
            if event.event_type == 'broker_submit_requested':
                submit_attempted = True
                _emit(events, 'submit_attempt', **event.details)
            elif event.event_type.startswith('broker_'):
                _emit(events, 'broker_update', source_event=event.event_type, **event.details)

        if not submit_attempted:
            reasons = list(execution.readiness.reasons if execution.readiness is not None else readiness.reasons)
            _emit(events, 'submit_skipped', reasons=reasons)

        terminal_details = self._terminal_details(execution)
        _emit(events, 'terminal_outcome', **terminal_details)
        self._write_events(events)
        final_status = execution.readiness.status if execution.readiness is not None else readiness.status
        final_reasons = execution.readiness.reasons if execution.readiness is not None else readiness.reasons
        return FirstLiveTradeResult(
            status=final_status,
            reasons=tuple(final_reasons),
            submit_attempted=submit_attempted,
            execution_outcome=terminal_details['outcome'],
            events=tuple(events),
        )

    def _blocked_result(self, events: list[FirstLiveTradeEvent], *reasons: str) -> FirstLiveTradeResult:
        _emit(events, 'probe_result', status='blocked', reasons=list(reasons))
        _emit(events, 'terminal_outcome', outcome='no_submit', reasons=list(reasons))
        self._write_events(events)
        return FirstLiveTradeResult(
            status='blocked',
            reasons=tuple(reasons),
            submit_attempted=False,
            execution_outcome='no_submit',
            events=tuple(events),
        )

    def _write_events(self, events: list[FirstLiveTradeEvent]) -> None:
        if self._event_log_path is not None:
            _write_event_log(events, self._event_log_path)

    @staticmethod
    def _terminal_details(execution) -> dict[str, Any]:
        reason = None
        for event in reversed(execution.events):
            if event.event_type == 'execution_terminal':
                reason = event.details.get('reason')
                break
        return {
            'outcome': execution.outcome or 'unknown',
            'reason': reason,
        }


def run_first_live_trade(
    config_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    session_verifier: SessionVerifier | None = None,
    adapter: BrokerAdapter | None = None,
    event_log_path: Path | None = None,
    defaults_path: Path = DEFAULTS_PATH,
) -> FirstLiveTradeResult:
    return FirstLiveTradeRunner(
        config_path,
        env=env,
        session_verifier=session_verifier,
        adapter=adapter,
        event_log_path=event_log_path,
        defaults_path=defaults_path,
    ).run()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run exactly one guarded first live trade attempt')
    parser.add_argument('--config', required=True, type=Path, help='Path to the first live trade config JSON')
    parser.add_argument('--event-log', type=Path, help='Optional JSONL path for append-only trade events')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_first_live_trade(args.config, event_log_path=args.event_log)
    print(result.status)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
