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
from scripts.run_first_live_trade import FirstLiveTradeRunner, run_first_live_trade


class StubVerifier:
    def __init__(self, *, verified: bool = True, reason: str | None = None) -> None:
        self.verified = verified
        self.reason = reason
        self.calls = 0

    def verify_session(self, _config: ClobAuthConfig) -> SessionVerificationResult:
        self.calls += 1
        return SessionVerificationResult(verified=self.verified, reason=self.reason)


class StubAdapter:
    def __init__(
        self,
        *,
        balance: float = 20.0,
        balance_age_seconds: float = 5.0,
        place_result: BrokerOrder | None = None,
    ) -> None:
        self.balance = BalanceSnapshot(
            available_usdc=balance,
            total_exposure=0.0,
            fetched_at=(datetime.now(timezone.utc) - timedelta(seconds=balance_age_seconds)).isoformat(),
        )
        self.place_result = place_result or BrokerOrder(
            order_id='ord-1',
            market_id='btc-5m',
            side='yes',
            amount=1.5,
            status='acknowledged',
        )
        self.place_order_calls = 0
        self.last_amount = None

    def fetch_balance(self) -> BalanceSnapshot:
        return self.balance

    def place_order(self, **kwargs) -> BrokerOrder:
        self.place_order_calls += 1
        self.last_amount = kwargs.get('amount')
        return self.place_result


def _write_config(tmp_path: Path, **overrides) -> Path:
    now = datetime.now(timezone.utc)
    payload = {
        'requested_mode': 'live',
        'live_trading_enabled': True,
        'market_id': 'btc-5m',
        'market_observed_at': (now - timedelta(seconds=5)).isoformat(),
        'health_state': 'ok',
        'momentum': 0.0040,
        'market_price': 99.6,
        'reference_price': 100.0,
        'yes_pressure': 80.0,
        'no_pressure': 20.0,
        'requested_notional_usd': 4.0,
        'max_first_trade_notional_usd': 1.5,
        'max_size': 5.0,
        'max_balance_fraction': 1.0,
    }
    payload.update(overrides)
    path = tmp_path / 'first-live.json'
    path.write_text(json.dumps(payload))
    return path


def _env(**overrides) -> dict[str, str]:
    payload = {
        'CLOB_API_KEY': 'key',
        'CLOB_API_SECRET': 'secret',
        'CLOB_API_PASSPHRASE': 'passphrase',
        'CLOB_SIGNER_ADDRESS': '0x' + ('1' * 40),
        'CLOB_SIGNER_KEY': '0x' + ('2' * 64),
        'CLOB_CHAIN_ID': '137',
        'CLOB_SIGNATURE_TYPE': '1',
        'CLOB_SESSION_VERIFIED': '1',
    }
    payload.update(overrides)
    return payload


def test_ready_live_path_reaches_single_guarded_submit_attempt(tmp_path: Path):
    config_path = _write_config(tmp_path)
    adapter = StubAdapter()
    event_log = tmp_path / 'first-live.jsonl'

    result = run_first_live_trade(
        config_path,
        env=_env(),
        session_verifier=StubVerifier(verified=True),
        adapter=adapter,
        event_log_path=event_log,
    )

    assert result.status == 'ready_live'
    assert result.submit_attempted is True
    assert result.execution_outcome == 'acknowledged'
    assert adapter.place_order_calls == 1
    event_types = [event.event_type for event in result.events]
    assert event_types[:4] == [
        'probe_result',
        'decision_recorded',
        'sizing_recorded',
        'submit_attempt',
    ]
    assert event_types.count('broker_update') == 3
    assert event_types[-1] == 'terminal_outcome'
    assert len(event_log.read_text().splitlines()) == len(result.events)


def test_non_ready_path_records_no_submit_with_explicit_reason(tmp_path: Path):
    config_path = _write_config(tmp_path, health_state='failed')
    adapter = StubAdapter()

    result = run_first_live_trade(
        config_path,
        env=_env(),
        session_verifier=StubVerifier(verified=True),
        adapter=adapter,
    )

    assert result.status == 'blocked'
    assert 'health_state:failed' in result.reasons
    assert result.submit_attempted is False
    assert adapter.place_order_calls == 0
    assert result.events[-2].event_type == 'submit_skipped'
    assert result.events[-1].event_type == 'terminal_outcome'


def test_max_notional_cap_is_enforced(tmp_path: Path):
    config_path = _write_config(tmp_path, requested_notional_usd=4.0, max_first_trade_notional_usd=1.25)
    adapter = StubAdapter(place_result=BrokerOrder(order_id='ord-1', market_id='btc-5m', side='yes', amount=1.25, status='acknowledged'))

    result = run_first_live_trade(
        config_path,
        env=_env(),
        session_verifier=StubVerifier(verified=True),
        adapter=adapter,
    )

    assert result.status == 'ready_live'
    assert adapter.last_amount == 1.25


def test_no_second_submit_can_occur_in_same_run(tmp_path: Path):
    config_path = _write_config(tmp_path)
    adapter = StubAdapter()
    runner = FirstLiveTradeRunner(
        config_path,
        env=_env(),
        session_verifier=StubVerifier(verified=True),
        adapter=adapter,
    )

    first = runner.run()
    second = runner.run()

    assert first.submit_attempted is True
    assert second.status == 'blocked'
    assert second.reasons == ('trade_already_attempted',)
    assert adapter.place_order_calls == 1


def test_terminal_outcome_is_always_recorded(tmp_path: Path):
    config_path = _write_config(tmp_path)
    adapter = StubAdapter(balance_age_seconds=120.0)

    result = run_first_live_trade(
        config_path,
        env=_env(),
        session_verifier=StubVerifier(verified=True),
        adapter=adapter,
    )

    assert result.events[-1].event_type == 'terminal_outcome'
    assert result.execution_outcome == 'no_trade'
