from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence


def parse_utc_timestamp(value: str | datetime) -> datetime:
    """Normalize timestamps to timezone-aware UTC datetimes.

    Raises:
        ValueError: when the timestamp is malformed.
    """
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(cleaned)
        except ValueError as exc:
            raise ValueError(f"Malformed timestamp: {value}") from exc
    else:
        raise ValueError(f"Malformed timestamp: {value}")

    if ts.tzinfo is None:
        raise ValueError(f"Malformed timestamp: {value}")
    return ts.astimezone(timezone.utc)


@dataclass(frozen=True)
class MarketMetadata:
    market_id: str
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    open_time: datetime
    close_time: datetime
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_time", parse_utc_timestamp(self.open_time))
        object.__setattr__(self, "close_time", parse_utc_timestamp(self.close_time))


@dataclass(frozen=True)
class OrderBookSnapshot:
    token_id: str
    ts_utc: datetime
    best_bid: float
    best_ask: float
    bids: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    asks: tuple[tuple[float, float], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_utc", parse_utc_timestamp(self.ts_utc))

    def is_stale(self, now: datetime, max_age_seconds: float) -> bool:
        now_utc = parse_utc_timestamp(now)
        age = (now_utc - self.ts_utc).total_seconds()
        return age > max_age_seconds


@dataclass(frozen=True)
class TradeTick:
    token_id: str
    ts_utc: datetime
    price: float
    size: float
    side: str
    trade_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_utc", parse_utc_timestamp(self.ts_utc))


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    ts_utc: datetime
    price: float
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_utc", parse_utc_timestamp(self.ts_utc))

    def is_stale(self, now: datetime, max_age_seconds: float) -> bool:
        now_utc = parse_utc_timestamp(now)
        age = (now_utc - self.ts_utc).total_seconds()
        return age > max_age_seconds


@dataclass(frozen=True)
class BrokerOrderIntent:
    round_id: str
    market_id: str
    token_id: str
    side: str
    limit_price: float
    max_notional_usd: float
    expected_edge: float
    expected_confidence: float
    idempotency_key: str
    reason: str


@dataclass(frozen=True)
class BrokerFillUpdate:
    order_id: str
    status: str
    filled_size: float
    avg_price: float | None
    ts_utc: datetime
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_utc", parse_utc_timestamp(self.ts_utc))


@dataclass(frozen=True)
class BalanceSnapshot:
    account_id: str
    ts_utc: datetime
    cash_usd: float
    reserved_usd: float
    available_usd: float
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_utc", parse_utc_timestamp(self.ts_utc))


class MarketMetadataAdapter(Protocol):
    def list_open_markets(self) -> Sequence[MarketMetadata]: ...

    def get_market(self, market_id: str) -> MarketMetadata: ...

    def healthcheck(self) -> Mapping[str, Any]: ...


class OrderBookAdapter(Protocol):
    def get_orderbook(self, token_id: str) -> OrderBookSnapshot: ...

    def healthcheck(self) -> Mapping[str, Any]: ...


class TradeFeedAdapter(Protocol):
    def get_recent_trades(self, token_id: str, limit: int = 100) -> Sequence[TradeTick]: ...

    def healthcheck(self) -> Mapping[str, Any]: ...


class PriceAdapter(Protocol):
    def get_latest_price(self, symbol: str) -> PriceTick: ...

    def get_price_series(self, symbol: str, interval: str, limit: int) -> Sequence[PriceTick]: ...

    def healthcheck(self) -> Mapping[str, Any]: ...


class BalanceSource(Protocol):
    def get_balance_snapshot(self) -> BalanceSnapshot: ...


class Broker(Protocol):
    def sync_balance(self, source: BalanceSource) -> BalanceSnapshot: ...

    def place_order(self, intent: BrokerOrderIntent) -> BrokerFillUpdate: ...

    def cancel(self, order_id: str) -> BrokerFillUpdate: ...

    def poll(self, order_id: str) -> BrokerFillUpdate: ...

    def healthcheck(self) -> Mapping[str, Any]: ...


class ReplaySource(Protocol):
    def load_round(self, round_id: str) -> Mapping[str, Any]: ...

    def list_rounds(self, start: datetime, end: datetime) -> Sequence[str]: ...
