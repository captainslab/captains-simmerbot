from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


def _is_non_empty(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_hex(value: str | None, *, expected_length: int) -> bool:
    if not _is_non_empty(value):
        return False
    normalized = value.strip().lower()
    if not normalized.startswith('0x') or len(normalized) != expected_length:
        return False
    return all(char in '0123456789abcdef' for char in normalized[2:])


@dataclass(frozen=True)
class ClobAuthConfig:
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    signer_address: str | None = None
    signer_key: str | None = None
    chain_id: int | None = 137
    signature_type: int | None = 1


@dataclass(frozen=True)
class SessionVerificationResult:
    verified: bool
    reason: str | None = None


@dataclass(frozen=True)
class AuthValidationResult:
    status: str
    reasons: tuple[str, ...] = ()


class SessionVerifier(Protocol):
    def verify_session(self, config: ClobAuthConfig) -> SessionVerificationResult: ...


class ClobAuthValidator:
    def __init__(
        self,
        *,
        session_verifier: SessionVerifier | None = None,
        required_chain_id: int = 137,
        allowed_signature_types: tuple[int, ...] = (1, 2),
    ) -> None:
        self._session_verifier = session_verifier
        self._required_chain_id = required_chain_id
        self._allowed_signature_types = allowed_signature_types

    def validate(self, config: ClobAuthConfig) -> AuthValidationResult:
        missing: list[str] = []
        if not _is_non_empty(config.api_key):
            missing.append('missing_api_key')
        if not _is_non_empty(config.api_secret):
            missing.append('missing_api_secret')
        if not _is_non_empty(config.api_passphrase):
            missing.append('missing_api_passphrase')
        if not _is_non_empty(config.signer_address):
            missing.append('missing_signer_address')
        if not _is_non_empty(config.signer_key):
            missing.append('missing_signer_key')
        if missing:
            return AuthValidationResult(status='auth_dry_run_only', reasons=tuple(missing))

        blocked: list[str] = []
        if config.chain_id != self._required_chain_id:
            blocked.append(f'invalid_chain_id:{config.chain_id}')
        if config.signature_type not in self._allowed_signature_types:
            blocked.append(f'invalid_signature_type:{config.signature_type}')
        if not _is_hex(config.signer_address, expected_length=42):
            blocked.append('invalid_signer_address')
        if not _is_hex(config.signer_key, expected_length=66):
            blocked.append('invalid_signer_key')
        if blocked:
            return AuthValidationResult(status='auth_blocked', reasons=tuple(blocked))

        verification = (
            self._session_verifier.verify_session(config)
            if self._session_verifier is not None
            else SessionVerificationResult(verified=True)
        )
        if not verification.verified:
            return AuthValidationResult(
                status='auth_blocked',
                reasons=(verification.reason or 'session_not_verified',),
            )
        return AuthValidationResult(status='auth_ready', reasons=())
