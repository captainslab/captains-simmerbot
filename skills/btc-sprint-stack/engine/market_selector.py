from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "adapters"
if str(ADAPTERS_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTERS_DIR))

from contracts import MarketMetadata, MarketMetadataAdapter, parse_utc_timestamp


class MarketSelectionError(RuntimeError):
    pass


class MalformedMarketError(MarketSelectionError):
    pass


class WrongMarketWindowError(MarketSelectionError):
    pass


class TokenMappingError(MarketSelectionError):
    pass


class StaleOrClosedMarketError(MarketSelectionError):
    pass


class AmbiguousMarketSelectionError(MarketSelectionError):
    pass


class NoEligibleMarketError(MarketSelectionError):
    pass


@dataclass(frozen=True)
class RejectedMarket:
    market_id: str
    reason: str


def _is_btc_market(market: MarketMetadata) -> bool:
    return "btc" in market.question.lower()


def validate_market(
    market: MarketMetadata,
    *,
    now: datetime,
    required_window: timedelta = timedelta(minutes=15),
) -> None:
    now_utc = parse_utc_timestamp(now)

    if not market.market_id or not market.condition_id:
        raise MalformedMarketError("Missing market_id or condition_id")

    if not market.yes_token_id or not market.no_token_id:
        raise TokenMappingError(f"{market.market_id}: missing YES/NO token mapping")
    if market.yes_token_id == market.no_token_id:
        raise TokenMappingError(f"{market.market_id}: ambiguous YES/NO token mapping")

    if market.close_time <= market.open_time:
        raise MalformedMarketError(f"{market.market_id}: close_time must be after open_time")

    window = market.close_time - market.open_time
    if window != required_window:
        raise WrongMarketWindowError(
            f"{market.market_id}: expected {required_window}, got {window}"
        )

    if now_utc >= market.close_time or now_utc < market.open_time:
        raise StaleOrClosedMarketError(
            f"{market.market_id}: market inactive for now={now_utc.isoformat()}"
        )


def select_current_market(
    adapter: MarketMetadataAdapter,
    *,
    now: datetime | None = None,
) -> MarketMetadata:
    now_utc = parse_utc_timestamp(now or datetime.now(timezone.utc))

    try:
        candidates = list(adapter.list_open_markets())
    except ValueError as exc:
        raise MalformedMarketError(str(exc)) from exc

    rejected: list[RejectedMarket] = []
    eligible: list[MarketMetadata] = []
    for market in candidates:
        try:
            if not _is_btc_market(market):
                raise NoEligibleMarketError(f"{market.market_id}: non-BTC market")
            validate_market(market, now=now_utc)
            eligible.append(market)
        except MarketSelectionError as exc:
            rejected.append(RejectedMarket(market_id=market.market_id, reason=str(exc)))

    if not eligible:
        details = ", ".join(f"{row.market_id}:{row.reason}" for row in rejected) or "no candidates"
        raise NoEligibleMarketError(f"No eligible BTC 15m market. rejects={details}")

    eligible.sort(key=lambda m: (m.close_time, m.market_id))
    winner = eligible[0]

    same_close = [m for m in eligible if m.close_time == winner.close_time]
    if len(same_close) > 1:
        ids = ", ".join(m.market_id for m in same_close)
        raise AmbiguousMarketSelectionError(f"Multiple markets share close time {winner.close_time}: {ids}")

    return winner
