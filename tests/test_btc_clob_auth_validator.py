from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from adapters.clob_auth_validator import (
    ClobAuthConfig,
    ClobAuthValidator,
    SessionVerificationResult,
)


class StubVerifier:
    def __init__(self, *, verified: bool = True, reason: str | None = None) -> None:
        self.verified = verified
        self.reason = reason
        self.calls = 0

    def verify_session(self, _config: ClobAuthConfig) -> SessionVerificationResult:
        self.calls += 1
        return SessionVerificationResult(verified=self.verified, reason=self.reason)


def _valid_config(**overrides) -> ClobAuthConfig:
    payload = {
        'api_key': 'key',
        'api_secret': 'secret',
        'api_passphrase': 'passphrase',
        'signer_address': '0x' + ('1' * 40),
        'signer_key': '0x' + ('2' * 64),
        'chain_id': 137,
        'signature_type': 1,
    }
    payload.update(overrides)
    return ClobAuthConfig(**payload)


def test_valid_auth_config_path_returns_auth_ready():
    verifier = StubVerifier(verified=True)
    validator = ClobAuthValidator(session_verifier=verifier)

    result = validator.validate(_valid_config())

    assert result.status == 'auth_ready'
    assert result.reasons == ()
    assert verifier.calls == 1


def test_missing_credential_path_returns_auth_dry_run_only():
    validator = ClobAuthValidator(session_verifier=StubVerifier())

    result = validator.validate(_valid_config(api_key=None))

    assert result.status == 'auth_dry_run_only'
    assert 'missing_api_key' in result.reasons


def test_invalid_signing_config_path_returns_auth_blocked():
    validator = ClobAuthValidator(session_verifier=StubVerifier())

    result = validator.validate(_valid_config(chain_id=1, signer_address='bad-address'))

    assert result.status == 'auth_blocked'
    assert 'invalid_chain_id:1' in result.reasons
    assert 'invalid_signer_address' in result.reasons


def test_failed_session_verification_returns_auth_blocked():
    validator = ClobAuthValidator(
        session_verifier=StubVerifier(verified=False, reason='session_not_verified'),
    )

    result = validator.validate(_valid_config())

    assert result.status == 'auth_blocked'
    assert result.reasons == ('session_not_verified',)
