from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "skills" / "btc-sprint-stack" / "modules"
ADAPTERS = ROOT / "skills" / "btc-sprint-stack" / "adapters"

for path in (MODULES, ADAPTERS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from contracts import BalanceSnapshot, PriceTick, parse_utc_timestamp


@pytest.fixture
def malformed_timestamp_fixture() -> dict:
    return {
        "bad": "2026/01/01 00:00:00",  # slash format is intentionally rejected
        "good_now": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    }


@pytest.fixture
def stale_feed_fixture() -> dict:
    now = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)
    return {
        "now": now,
        "fresh": PriceTick(symbol="BTCUSD", ts_utc=now - timedelta(seconds=5), price=45000.0, source="fixture"),
        "stale": PriceTick(symbol="BTCUSD", ts_utc=now - timedelta(seconds=40), price=44900.0, source="fixture"),
        "max_age_seconds": 20,
    }


@pytest.fixture
def balance_fixture() -> dict:
    return {
        "snapshot": BalanceSnapshot(
            account_id="paper-account",
            ts_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            cash_usd=60.0,
            reserved_usd=5.0,
            available_usd=55.0,
            source="exchange_truth",
        )
    }


def test_engine_modules_do_not_import_external_clients_directly():
    target_modules = {
        "btc_sprint_signal.py",
        "btc_sprint_executor.py",
        "btc_position_manager.py",
        "btc_regime_filter.py",
        "btc_heartbeat.py",
    }
    banned_prefixes = (
        "py_clob_client",
        "chainlink",
        "requests",
        "httpx",
        "websocket",
        "websockets",
        "ccxt",
        "binance",
        "coinbase",
        "kraken",
        "alchemy",
        "web3",
        "urllib.request",
    )

    violations: list[str] = []
    for module_path in sorted(MODULES.glob("*.py")):
        if module_path.name not in target_modules:
            continue
        tree = ast.parse(module_path.read_text(), filename=str(module_path))
        imported_contracts = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_contracts = imported_contracts or (node.module == "contracts")
                if node.module and node.module.startswith(banned_prefixes):
                    violations.append(f"{module_path.name}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(banned_prefixes):
                        violations.append(f"{module_path.name}: import {alias.name}")
        if module_path.name == "btc_sprint_signal.py":
            assert imported_contracts, "btc_sprint_signal.py must import adapter contracts"

    assert not violations, "\n".join(violations)


def test_contracts_reject_malformed_timestamp(malformed_timestamp_fixture: dict):
    with pytest.raises(ValueError, match="Malformed timestamp"):
        parse_utc_timestamp(malformed_timestamp_fixture["bad"])

    assert parse_utc_timestamp(malformed_timestamp_fixture["good_now"]).tzinfo is not None


def test_stale_feed_fixture_supports_no_trade_gate(stale_feed_fixture: dict):
    now = stale_feed_fixture["now"]
    max_age = stale_feed_fixture["max_age_seconds"]

    assert stale_feed_fixture["fresh"].is_stale(now, max_age) is False
    assert stale_feed_fixture["stale"].is_stale(now, max_age) is True


@dataclass
class FakeBalanceSource:
    snapshot: BalanceSnapshot

    def get_balance_snapshot(self) -> BalanceSnapshot:
        return self.snapshot


@dataclass
class FakeBroker:
    internal_balance_usd: float

    def sync_balance(self, source: FakeBalanceSource) -> BalanceSnapshot:
        # source-of-truth comes from source snapshot, not internal mutable number
        return source.get_balance_snapshot()


def test_broker_balance_source_of_truth_separation(balance_fixture: dict):
    source = FakeBalanceSource(snapshot=balance_fixture["snapshot"])
    broker = FakeBroker(internal_balance_usd=999999.0)

    synced = broker.sync_balance(source)
    assert synced.available_usd == 55.0
    assert synced.source == "exchange_truth"
    assert synced.available_usd != broker.internal_balance_usd
