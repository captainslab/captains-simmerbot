from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from adapters.clob_auth_validator import ClobAuthConfig, SessionVerificationResult
from adapters.polymarket_clob import BalanceSnapshot, BrokerOrder
from execution.session_controller import (
    SessionController,
    SessionControllerConfig,
    SessionRoundOutcome,
    SessionRoundSpec,
)
from scripts.run_live_session import run_live_session


class StubProcessor:
    def __init__(self, outcomes: list[SessionRoundOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[str] = []

    def process_round(self, round_spec: SessionRoundSpec) -> SessionRoundOutcome:
        self.calls.append(round_spec.round_id)
        return self._outcomes.pop(0)


class StubVerifier:
    def verify_session(self, _config: ClobAuthConfig) -> SessionVerificationResult:
        return SessionVerificationResult(verified=True)


class SequenceAdapter:
    def __init__(self, *, balances: list[float], trade_amount: float = 1.0) -> None:
        self._balances = list(balances)
        self._last_balance = balances[-1]
        self.trade_amount = trade_amount
        self.place_order_calls = 0

    def fetch_balance(self) -> BalanceSnapshot:
        if self._balances:
            self._last_balance = self._balances.pop(0)
        return BalanceSnapshot(
            available_usdc=self._last_balance,
            total_exposure=0.0,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    def place_order(self, **kwargs) -> BrokerOrder:
        self.place_order_calls += 1
        return BrokerOrder(
            order_id=f'ord-{self.place_order_calls}',
            market_id=str(kwargs['market_id']),
            side=str(kwargs['side']),
            amount=float(kwargs['amount']),
            status='filled',
            filled_amount=float(kwargs['amount']),
            remaining_amount=0.0,
        )

    def fetch_order_status(self, order_id: str) -> BrokerOrder:
        return BrokerOrder(
            order_id=order_id,
            market_id='btc-5m',
            side='yes',
            amount=self.trade_amount,
            status='filled',
            filled_amount=self.trade_amount,
            remaining_amount=0.0,
        )


def _round(round_id: str, *, requested_notional: float = 1.0) -> SessionRoundSpec:
    return SessionRoundSpec(round_id=round_id, requested_notional=requested_notional, payload={})


def _attempted(
    *,
    attempted_notional: float = 1.0,
    execution_outcome: str = 'filled',
    loss: bool = False,
    reconciliation_status: str = 'reconciled',
    stop_reason: str | None = None,
) -> SessionRoundOutcome:
    return SessionRoundOutcome(
        session_action='trade_attempted',
        attempted_notional=attempted_notional,
        round_status='ready_live',
        execution_outcome=execution_outcome,
        reasons=(),
        reconciliation_status=reconciliation_status,
        reconciliation_reasons=(),
        stop_reason=stop_reason,
        loss=loss,
    )


def _skipped(*, stop_reason: str | None = None, reason: str = 'no_trade_action') -> SessionRoundOutcome:
    return SessionRoundOutcome(
        session_action='trade_skipped',
        round_status='ready_dry_run' if stop_reason is None else 'blocked',
        reasons=(reason,),
        stop_reason=stop_reason,
    )


def _write_session_config(tmp_path: Path, *, rounds: int = 3, trade_amount: float = 1.0) -> Path:
    now = datetime.now(timezone.utc)
    round_payload = {
        'requested_mode': 'live',
        'live_trading_enabled': True,
        'market_id': 'btc-5m',
        'market_observed_at': (now - timedelta(seconds=5)).isoformat(),
        'feed_observed_at': (now - timedelta(seconds=5)).isoformat(),
        'health_state': 'ok',
        'momentum': 0.0040,
        'market_price': 99.6,
        'reference_price': 100.0,
        'yes_pressure': 80.0,
        'no_pressure': 20.0,
        'requested_notional_usd': trade_amount,
        'max_first_trade_notional_usd': trade_amount,
        'max_size': 5.0,
        'max_balance_fraction': 1.0,
    }
    payload = {
        'session_id': 'session-1',
        'requested_mode': 'live',
        'live_trading_enabled': True,
        'max_trades_per_session': 2,
        'max_notional_per_session': 5.0,
        'max_consecutive_losses': 2,
        'rounds': [dict(round_payload) for _ in range(rounds)],
    }
    path = tmp_path / 'live-session.json'
    path.write_text(json.dumps(payload))
    return path


def _env() -> dict[str, str]:
    return {
        'CLOB_API_KEY': 'key',
        'CLOB_API_SECRET': 'secret',
        'CLOB_API_PASSPHRASE': 'passphrase',
        'CLOB_SIGNER_ADDRESS': '0x' + ('1' * 40),
        'CLOB_SIGNER_KEY': '0x' + ('2' * 64),
        'CLOB_CHAIN_ID': '137',
        'CLOB_SIGNATURE_TYPE': '1',
        'CLOB_SESSION_VERIFIED': '1',
    }


def test_session_stops_at_max_trades(tmp_path: Path):
    config_path = _write_session_config(tmp_path, rounds=3, trade_amount=1.0)
    adapter = SequenceAdapter(balances=[10.0, 10.0, 10.0, 9.0, 9.0, 9.0, 9.0, 8.0])

    result = run_live_session(
        config_path,
        env=_env(),
        session_verifier=StubVerifier(),
        adapter=adapter,
    )

    assert result.stop_reason == 'max_trades_per_session_reached'
    assert result.trades_attempted == 2
    assert adapter.place_order_calls == 2
    assert [event.event_type for event in result.events] == [
        'session_started',
        'trade_attempted',
        'trade_attempted',
        'session_stopped',
    ]


def test_session_stops_at_max_notional():
    processor = StubProcessor(
        [
            _attempted(attempted_notional=1.25),
            _attempted(attempted_notional=1.25),
            _attempted(attempted_notional=1.25),
        ]
    )
    controller = SessionController(
        config=SessionControllerConfig(
            max_trades_per_session=5,
            max_notional_per_session=2.5,
            max_consecutive_losses=3,
        ),
        round_processor=processor,
    )

    result = controller.run([_round('r1', requested_notional=1.25), _round('r2', requested_notional=1.25), _round('r3', requested_notional=1.25)], session_id='s1')

    assert result.stop_reason == 'max_notional_per_session_reached'
    assert result.total_notional == 2.5
    assert processor.calls == ['r1', 'r2']


def test_session_stops_after_consecutive_losses_threshold():
    processor = StubProcessor(
        [
            _attempted(execution_outcome='failed', loss=True),
            _attempted(execution_outcome='rejected', loss=True),
            _attempted(),
        ]
    )
    controller = SessionController(
        config=SessionControllerConfig(
            max_trades_per_session=5,
            max_notional_per_session=10.0,
            max_consecutive_losses=2,
        ),
        round_processor=processor,
    )

    result = controller.run([_round('r1'), _round('r2'), _round('r3')], session_id='s1')

    assert result.stop_reason == 'max_consecutive_losses_reached'
    assert result.consecutive_losses == 2
    assert processor.calls == ['r1', 'r2']


def test_reconciliation_mismatch_unresolved_stops_session():
    for reconciliation_status in ('mismatch', 'unresolved'):
        processor = StubProcessor(
            [
                _attempted(
                    execution_outcome='filled',
                    reconciliation_status=reconciliation_status,
                    stop_reason=f'reconciliation_{reconciliation_status}',
                ),
                _attempted(),
            ]
        )
        controller = SessionController(
            config=SessionControllerConfig(
                max_trades_per_session=5,
                max_notional_per_session=10.0,
                max_consecutive_losses=3,
            ),
            round_processor=processor,
        )

        result = controller.run([_round('r1'), _round('r2')], session_id='s1')

        assert result.stop_reason == f'reconciliation_{reconciliation_status}'
        assert processor.calls == ['r1']


def test_stale_auth_readiness_failure_stops_session():
    for reason in ('stale_market:61.0s', 'stale_balance:61.0s', 'stale_feed:61.0s', 'missing_api_key', 'health_state:failed'):
        processor = StubProcessor([_skipped(stop_reason=reason, reason=reason), _attempted()])
        controller = SessionController(
            config=SessionControllerConfig(
                max_trades_per_session=5,
                max_notional_per_session=10.0,
                max_consecutive_losses=3,
            ),
            round_processor=processor,
        )

        result = controller.run([_round('r1'), _round('r2')], session_id='s1')

        assert result.stop_reason == reason
        assert processor.calls == ['r1']


def test_skipped_rounds_do_not_bypass_stop_logic():
    processor = StubProcessor(
        [
            _attempted(execution_outcome='failed', loss=True),
            _skipped(),
            _attempted(execution_outcome='failed', loss=True),
            _attempted(),
        ]
    )
    controller = SessionController(
        config=SessionControllerConfig(
            max_trades_per_session=5,
            max_notional_per_session=10.0,
            max_consecutive_losses=2,
        ),
        round_processor=processor,
    )

    result = controller.run([_round('r1'), _round('r2'), _round('r3'), _round('r4')], session_id='s1')

    assert result.stop_reason == 'max_consecutive_losses_reached'
    assert result.consecutive_losses == 2
    assert processor.calls == ['r1', 'r2', 'r3']


def test_terminal_session_stopped_event_always_recorded():
    processor = StubProcessor([])
    controller = SessionController(
        config=SessionControllerConfig(
            max_trades_per_session=1,
            max_notional_per_session=1.0,
            max_consecutive_losses=1,
        ),
        round_processor=processor,
    )

    result = controller.run([], session_id='s1')

    assert result.events[-1].event_type == 'session_stopped'
