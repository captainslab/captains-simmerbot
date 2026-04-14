from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = ROOT / "skills" / "btc-sprint-stack" / "adapters"
ENGINE = ROOT / "skills" / "btc-sprint-stack" / "engine"
for path in (ADAPTERS, ENGINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from contracts import MarketMetadata
from market_selector import (
    AmbiguousMarketSelectionError,
    MalformedMarketError,
    NoEligibleMarketError,
    select_current_market,
)


@dataclass
class FakeAdapter:
    markets: list[MarketMetadata] | None = None
    error: Exception | None = None

    def list_open_markets(self):
        if self.error:
            raise self.error
        return list(self.markets or [])

    def get_market(self, market_id: str) -> MarketMetadata:
        for market in self.markets or []:
            if market.market_id == market_id:
                return market
        raise KeyError(market_id)

    def healthcheck(self):
        return {"status": "ok"}


def _market(
    *,
    market_id: str = "m1",
    open_time: datetime,
    close_time: datetime,
    yes_token_id: str = "yes-token",
    no_token_id: str = "no-token",
    question: str = "Will BTC be above 50k in next 15m?",
) -> MarketMetadata:
    return MarketMetadata(
        market_id=market_id,
        condition_id=f"cond-{market_id}",
        question=question,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        open_time=open_time,
        close_time=close_time,
        tags=("btc", "15m"),
    )


def test_market_selector_happy_path_selects_valid_current_15m_market():
    now = datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc)
    market = _market(open_time=now - timedelta(minutes=7), close_time=now + timedelta(minutes=8))
    adapter = FakeAdapter(markets=[market])

    selected = select_current_market(adapter, now=now)
    assert selected.market_id == "m1"


def test_market_selector_malformed_timestamp_path_is_typed_failure():
    adapter = FakeAdapter(error=ValueError("Malformed timestamp: 2026/01/01 00:00:00"))
    now = datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc)

    with pytest.raises(MalformedMarketError, match="Malformed timestamp"):
        select_current_market(adapter, now=now)


def test_market_selector_rejects_non_15m_window():
    now = datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc)
    wrong_window = _market(open_time=now - timedelta(minutes=10), close_time=now + timedelta(minutes=10))
    adapter = FakeAdapter(markets=[wrong_window])

    with pytest.raises(NoEligibleMarketError, match="expected 0:15:00"):
        select_current_market(adapter, now=now)


def test_market_selector_rejects_missing_or_ambiguous_token_mapping():
    now = datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc)
    missing = _market(
        market_id="missing",
        open_time=now - timedelta(minutes=7),
        close_time=now + timedelta(minutes=8),
        yes_token_id="",
        no_token_id="",
    )
    ambiguous = _market(
        market_id="ambiguous",
        open_time=now - timedelta(minutes=7),
        close_time=now + timedelta(minutes=8),
        yes_token_id="same-token",
        no_token_id="same-token",
    )
    adapter = FakeAdapter(markets=[missing, ambiguous])

    with pytest.raises(NoEligibleMarketError, match="token mapping"):
        select_current_market(adapter, now=now)


def test_market_selector_rejects_stale_or_closed_market():
    now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    closed = _market(open_time=now - timedelta(minutes=20), close_time=now - timedelta(minutes=5))
    adapter = FakeAdapter(markets=[closed])

    with pytest.raises(NoEligibleMarketError, match="market inactive"):
        select_current_market(adapter, now=now)


def test_market_selector_multiple_candidates_same_close_time_is_typed_failure():
    now = datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc)
    one = _market(market_id="m1", open_time=now - timedelta(minutes=7), close_time=now + timedelta(minutes=8))
    two = _market(market_id="m2", open_time=now - timedelta(minutes=7), close_time=now + timedelta(minutes=8))
    adapter = FakeAdapter(markets=[one, two])

    with pytest.raises(AmbiguousMarketSelectionError, match="Multiple markets share close time"):
        select_current_market(adapter, now=now)
