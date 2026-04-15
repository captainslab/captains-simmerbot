from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.clob_auth_validator import SessionVerifier
from adapters.polymarket_clob import BalanceSnapshot, BrokerOrder
from execution.reconciliation import ReconciliationResult, order_intent_from_dict, reconcile_trade
from execution.session_controller import (
    SessionController,
    SessionControllerConfig,
    SessionResult,
    SessionRoundOutcome,
    SessionRoundProcessor,
    SessionRoundSpec,
)
from execution.order_state_machine import TERMINAL_STATES
from scripts.run_first_live_trade import (
    DEFAULTS_PATH,
    EnvironmentSessionVerifier,
    FirstLiveTradeResult,
    REQUIRED_FIELDS,
    _build_live_adapter,
    run_first_live_trade,
)


SESSION_REQUIRED_FIELDS = (
    'session_id',
    'max_trades_per_session',
    'max_notional_per_session',
    'max_consecutive_losses',
    'rounds',
)


class BrokerAdapter(Protocol):
    def fetch_balance(self) -> BalanceSnapshot: ...

    def place_order(self, **kwargs) -> BrokerOrder: ...

    def fetch_order_status(self, order_id: str) -> BrokerOrder: ...


@dataclass(frozen=True)
class LiveSessionConfig:
    session_id: str
    requested_mode: str
    live_trading_enabled: bool
    max_trades_per_session: int
    max_notional_per_session: float
    max_consecutive_losses: int
    max_feed_age_seconds: float
    rounds: tuple[SessionRoundSpec, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f'invalid_boolean:{field_name}')


def _parse_float(value: Any, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'invalid_float:{field_name}') from exc


def _parse_int(value: Any, *, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'invalid_integer:{field_name}') from exc


def _parse_timestamp(value: str | None, *, field_name: str) -> datetime:
    if not value:
        raise ValueError(f'missing_timestamp:{field_name}')
    normalized = value.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f'invalid_timestamp:{field_name}') from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_live_session_config(path: Path) -> LiveSessionConfig:
    payload = json.loads(path.read_text())
    missing = [field for field in SESSION_REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ValueError(','.join(f'missing_session_field:{field}' for field in missing))

    rounds_payload = payload['rounds']
    if not isinstance(rounds_payload, list) or not rounds_payload:
        raise ValueError('invalid_session_rounds')

    requested_mode = str(payload.get('requested_mode', 'live'))
    live_trading_enabled = _parse_bool(payload.get('live_trading_enabled', True), field_name='live_trading_enabled')
    max_feed_age_seconds = _parse_float(payload.get('max_feed_age_seconds', 30.0), field_name='max_feed_age_seconds')

    rounds: list[SessionRoundSpec] = []
    for index, round_payload in enumerate(rounds_payload, start=1):
        if not isinstance(round_payload, dict):
            raise ValueError(f'invalid_round_payload:{index}')
        merged = dict(round_payload)
        merged.setdefault('requested_mode', requested_mode)
        merged.setdefault('live_trading_enabled', live_trading_enabled)
        missing_round_fields = [field for field in REQUIRED_FIELDS if field not in merged]
        if missing_round_fields:
            raise ValueError(','.join(f'missing_round_field:{index}:{field}' for field in missing_round_fields))
        requested_notional = _parse_float(merged['requested_notional_usd'], field_name=f'round_{index}_requested_notional_usd')
        max_notional = _parse_float(
            merged['max_first_trade_notional_usd'],
            field_name=f'round_{index}_max_first_trade_notional_usd',
        )
        rounds.append(
            SessionRoundSpec(
                round_id=f"{payload['session_id']}:{index}",
                requested_notional=min(requested_notional, max_notional),
                payload=merged,
            )
        )

    return LiveSessionConfig(
        session_id=str(payload['session_id']),
        requested_mode=requested_mode,
        live_trading_enabled=live_trading_enabled,
        max_trades_per_session=_parse_int(payload['max_trades_per_session'], field_name='max_trades_per_session'),
        max_notional_per_session=_parse_float(payload['max_notional_per_session'], field_name='max_notional_per_session'),
        max_consecutive_losses=_parse_int(payload['max_consecutive_losses'], field_name='max_consecutive_losses'),
        max_feed_age_seconds=max_feed_age_seconds,
        rounds=tuple(rounds),
    )


class ObservedBrokerAdapter:
    def __init__(self, delegate: BrokerAdapter) -> None:
        self._delegate = delegate
        self.reset_round()

    def reset_round(self) -> None:
        self.last_balance: BalanceSnapshot | None = None
        self.pre_submit_balance: BalanceSnapshot | None = None
        self.last_placed_order: BrokerOrder | None = None

    def fetch_balance(self) -> BalanceSnapshot:
        balance = self._delegate.fetch_balance()
        self.last_balance = balance
        return balance

    def place_order(self, **kwargs) -> BrokerOrder:
        self.pre_submit_balance = self.last_balance
        self.last_placed_order = self._delegate.place_order(**kwargs)
        return self.last_placed_order

    def fetch_order_status(self, order_id: str) -> BrokerOrder:
        return self._delegate.fetch_order_status(order_id)


class LiveSessionRoundProcessor(SessionRoundProcessor):
    def __init__(
        self,
        *,
        adapter: ObservedBrokerAdapter,
        env: Mapping[str, str],
        session_verifier: SessionVerifier,
        max_feed_age_seconds: float,
        defaults_path: Path,
    ) -> None:
        self._adapter = adapter
        self._env = dict(env)
        self._session_verifier = session_verifier
        self._max_feed_age_seconds = max_feed_age_seconds
        self._defaults_path = defaults_path

    def process_round(self, round_spec: SessionRoundSpec) -> SessionRoundOutcome:
        self._adapter.reset_round()
        feed_reason = self._feed_failure_reason(round_spec.payload)
        if feed_reason is not None:
            return SessionRoundOutcome(
                session_action='trade_skipped',
                round_status='blocked',
                reasons=(feed_reason,),
                stop_reason=feed_reason,
            )

        with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.json', delete=True) as handle:
            json.dump(round_spec.payload, handle)
            handle.flush()
            result = run_first_live_trade(
                Path(handle.name),
                env=self._env,
                session_verifier=self._session_verifier,
                adapter=self._adapter,
                defaults_path=self._defaults_path,
            )

        stop_reason = self._round_stop_reason(result)
        if not result.submit_attempted:
            return SessionRoundOutcome(
                session_action='trade_skipped',
                round_status=result.status,
                execution_outcome=result.execution_outcome,
                reasons=tuple(result.reasons),
                stop_reason=stop_reason,
            )

        attempted_notional = self._extract_attempted_notional(result)
        reconciliation = self._reconcile_result(result)
        reconcile_stop_reason = None
        if reconciliation.status in {'mismatch', 'unresolved'}:
            reconcile_stop_reason = f'reconciliation_{reconciliation.status}'
        return SessionRoundOutcome(
            session_action='trade_attempted',
            attempted_notional=attempted_notional,
            round_status=result.status,
            execution_outcome=result.execution_outcome,
            reasons=tuple(result.reasons),
            reconciliation_status=reconciliation.status,
            reconciliation_reasons=tuple(reconciliation.reasons),
            stop_reason=reconcile_stop_reason or stop_reason,
            loss=result.execution_outcome in {'cancelled', 'rejected', 'failed'},
        )

    def _feed_failure_reason(self, payload: Mapping[str, Any]) -> str | None:
        try:
            observed_at = _parse_timestamp(
                str(payload.get('feed_observed_at') or payload.get('market_observed_at') or ''),
                field_name='feed_observed_at',
            )
        except ValueError as exc:
            return str(exc)

        max_feed_age = _parse_float(
            payload.get('max_feed_age_seconds', self._max_feed_age_seconds),
            field_name='max_feed_age_seconds',
        )
        feed_age = max(0.0, (_utc_now() - observed_at).total_seconds())
        if feed_age > max_feed_age:
            return f'stale_feed:{feed_age:.1f}s'
        return None

    @staticmethod
    def _round_stop_reason(result: FirstLiveTradeResult) -> str | None:
        if result.status == 'blocked':
            return result.reasons[0] if result.reasons else 'blocked'
        if result.status == 'ready_dry_run' and tuple(result.reasons) != ('no_trade_action',):
            return result.reasons[0] if result.reasons else 'ready_dry_run'
        return None

    @staticmethod
    def _extract_attempted_notional(result: FirstLiveTradeResult) -> float:
        for event in result.events:
            if event.event_type == 'submit_attempt':
                return float(event.details.get('amount', 0.0))
        return 0.0

    def _reconcile_result(self, result: FirstLiveTradeResult) -> ReconciliationResult:
        intent = self._intent_from_round_result(result)
        balance_before = self._adapter.pre_submit_balance or self._adapter.last_balance
        if intent is None or balance_before is None:
            return ReconciliationResult(
                status='unresolved',
                reasons=('missing_reconciliation_input',),
                balance_delta=0.0,
                events=(),
            )

        order_id = intent.provider_order_id
        broker_order = self._adapter.last_placed_order
        if broker_order is None:
            return ReconciliationResult(
                status='unresolved',
                reasons=('missing_broker_order',),
                balance_delta=0.0,
                events=(),
            )
        if broker_order.status not in TERMINAL_STATES:
            if order_id:
                broker_order = self._adapter.fetch_order_status(order_id)
            else:
                broker_order = None
        balance_after = self._adapter.fetch_balance()
        return reconcile_trade(
            intent=intent,
            broker_order=broker_order,
            balance_before=balance_before,
            balance_after=balance_after,
        )

    @staticmethod
    def _intent_from_round_result(result: FirstLiveTradeResult):
        broker_updates = [event for event in result.events if event.event_type == 'broker_update']
        if not broker_updates:
            return None
        created_event = next(
            (event for event in broker_updates if event.details.get('source_event') == 'broker_created'),
            None,
        )
        if created_event is None:
            return None

        last_event = broker_updates[-1]
        event_payloads = []
        for event in broker_updates:
            source_event = str(event.details.get('source_event', 'broker_created'))
            event_payloads.append(
                {
                    'order_id': event.details.get('order_id'),
                    'state': source_event.removeprefix('broker_'),
                    'timestamp': event.timestamp,
                    'details': {
                        key: value
                        for key, value in event.details.items()
                        if key != 'source_event'
                    },
                }
            )

        details = last_event.details
        return order_intent_from_dict(
            {
                'idempotency_key': str(created_event.details.get('idempotency_key')),
                'market_id': str(created_event.details.get('market_id')),
                'side': str(created_event.details.get('side')),
                'amount': float(created_event.details.get('amount', 0.0)),
                'state': str(last_event.details.get('source_event', 'broker_created')).removeprefix('broker_'),
                'provider_order_id': details.get('provider_order_id') or details.get('order_id'),
                'filled_amount': float(details.get('filled_amount', 0.0)),
                'remaining_amount': details.get('remaining_amount'),
                'reason': details.get('reason'),
                'balance_available': details.get('balance_available'),
                'events': event_payloads,
            }
        )


def _write_event_log(result: SessionResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for event in result.events:
            handle.write(json.dumps(asdict(event), sort_keys=True) + '\n')


def run_live_session(
    config_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    session_verifier: SessionVerifier | None = None,
    adapter: BrokerAdapter | None = None,
    event_log_path: Path | None = None,
    defaults_path: Path = DEFAULTS_PATH,
) -> SessionResult:
    session_config = load_live_session_config(config_path)
    resolved_env = dict(os.environ) if env is None else dict(env)
    resolved_adapter = ObservedBrokerAdapter(adapter or _build_live_adapter(resolved_env))
    processor = LiveSessionRoundProcessor(
        adapter=resolved_adapter,
        env=resolved_env,
        session_verifier=session_verifier or EnvironmentSessionVerifier(resolved_env),
        max_feed_age_seconds=session_config.max_feed_age_seconds,
        defaults_path=defaults_path,
    )
    controller = SessionController(
        config=SessionControllerConfig(
            max_trades_per_session=session_config.max_trades_per_session,
            max_notional_per_session=session_config.max_notional_per_session,
            max_consecutive_losses=session_config.max_consecutive_losses,
        ),
        round_processor=processor,
    )
    result = controller.run(list(session_config.rounds), session_id=session_config.session_id)
    if event_log_path is not None:
        _write_event_log(result, event_log_path)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run a capped guarded live trading session')
    parser.add_argument('--config', required=True, type=Path, help='Path to live session config JSON')
    parser.add_argument('--event-log', type=Path, help='Optional JSONL path for append-only session events')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_live_session(args.config, event_log_path=args.event_log)
    print(result.status)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
