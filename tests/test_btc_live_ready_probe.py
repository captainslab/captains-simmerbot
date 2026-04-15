from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from adapters.clob_auth_validator import ClobAuthConfig, SessionVerificationResult
from scripts.prove_live_ready import run_live_ready_probe


class StubVerifier:
    def __init__(self, *, verified: bool = True, reason: str | None = None) -> None:
        self.verified = verified
        self.reason = reason
        self.calls = 0

    def verify_session(self, _config: ClobAuthConfig) -> SessionVerificationResult:
        self.calls += 1
        return SessionVerificationResult(verified=self.verified, reason=self.reason)


def _write_config(tmp_path: Path, **overrides) -> Path:
    now = datetime.now(timezone.utc)
    payload = {
        'requested_mode': 'live',
        'live_trading_enabled': True,
        'market_id': 'btc-5m',
        'market_observed_at': (now - timedelta(seconds=5)).isoformat(),
        'balance_available_usdc': 20.0,
        'balance_fetched_at': (now - timedelta(seconds=5)).isoformat(),
        'health_state': 'ok',
        'action': 'buy_yes',
        'target_notional_usd': 4.0,
        'edge': 0.8,
    }
    payload.update(overrides)
    path = tmp_path / 'probe.json'
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


def test_ready_live_probe_path(tmp_path: Path):
    config_path = _write_config(tmp_path)
    verifier = StubVerifier(verified=True)
    event_log = tmp_path / 'probe.jsonl'

    result = run_live_ready_probe(
        config_path,
        env=_env(),
        session_verifier=verifier,
        event_log_path=event_log,
    )

    assert result.status == 'ready_live'
    assert result.reasons == ()
    assert verifier.calls == 1
    assert [event.event_type for event in result.events] == [
        'auth_evaluated',
        'sizing_evaluated',
        'readiness_evaluated',
        'probe_completed',
    ]
    assert len(event_log.read_text().splitlines()) == 4


def test_ready_dry_run_probe_path(tmp_path: Path):
    config_path = _write_config(tmp_path)

    result = run_live_ready_probe(
        config_path,
        env=_env(CLOB_API_KEY=''),
        session_verifier=StubVerifier(verified=True),
    )

    assert result.status == 'ready_dry_run'
    assert 'missing_api_key' in result.reasons


def test_blocked_probe_path(tmp_path: Path):
    config_path = _write_config(tmp_path, health_state='failed')

    result = run_live_ready_probe(
        config_path,
        env=_env(),
        session_verifier=StubVerifier(verified=True),
    )

    assert result.status == 'blocked'
    assert 'health_state:failed' in result.reasons


def test_missing_config_credential_path_returns_explicit_reason(tmp_path: Path):
    missing_path = tmp_path / 'missing.json'

    missing_config = run_live_ready_probe(
        missing_path,
        env=_env(),
        session_verifier=StubVerifier(verified=True),
    )
    missing_credential = run_live_ready_probe(
        _write_config(tmp_path),
        env=_env(CLOB_API_SECRET=''),
        session_verifier=StubVerifier(verified=True),
    )

    assert missing_config.status == 'blocked'
    assert missing_config.reasons == ('missing_config_path',)
    assert missing_credential.status == 'ready_dry_run'
    assert 'missing_api_secret' in missing_credential.reasons


def test_probe_never_places_or_submits_live_order(tmp_path: Path):
    config_path = _write_config(tmp_path)

    result = run_live_ready_probe(
        config_path,
        env=_env(),
        session_verifier=StubVerifier(verified=True),
    )

    assert result.status == 'ready_live'
    assert all('submit' not in event.event_type for event in result.events)
    assert all('broker' not in event.event_type for event in result.events)
