from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class BalanceSnapshot:
    available_usdc: float
    total_exposure: float
    fetched_at: str


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str | None
    market_id: str
    side: str
    amount: float
    status: str
    filled_amount: float = 0.0
    remaining_amount: float | None = None
    average_price: float | None = None
    reason: str | None = None


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class PolymarketClobAdapter:
    def __init__(self, client) -> None:
        self._client = client

    def fetch_balance(self) -> BalanceSnapshot:
        portfolio = self._client.get_portfolio()
        if not isinstance(portfolio, dict):
            raise RuntimeError('portfolio unavailable')
        available_usdc = _to_float(portfolio.get('balance_usdc'), default=-1.0)
        if available_usdc < 0:
            raise RuntimeError('portfolio missing balance_usdc')
        return BalanceSnapshot(
            available_usdc=available_usdc,
            total_exposure=_to_float(portfolio.get('total_exposure')),
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    def place_order(
        self,
        *,
        market_id: str,
        side: str,
        amount: float,
        order_type: str = 'FAK',
        reasoning: str | None = None,
        source: str | None = None,
        skill_slug: str | None = None,
        signal_data: dict[str, Any] | None = None,
    ) -> BrokerOrder:
        trade = self._client.trade(
            market_id=market_id,
            side=side,
            amount=amount,
            venue='polymarket',
            order_type=order_type,
            reasoning=reasoning,
            source=source,
            skill_slug=skill_slug,
            signal_data=signal_data,
        )
        filled_amount = _to_float(getattr(trade, 'shares_bought', 0.0))
        requested_amount = _to_float(getattr(trade, 'shares_requested', 0.0))
        remaining_amount = max(requested_amount - filled_amount, 0.0) if requested_amount else None
        status = self._normalize_trade_status(trade, filled_amount=filled_amount, requested_amount=requested_amount)
        return BrokerOrder(
            order_id=getattr(trade, 'trade_id', None),
            market_id=getattr(trade, 'market_id', market_id) or market_id,
            side=getattr(trade, 'side', side) or side,
            amount=_to_float(getattr(trade, 'cost', amount), default=amount),
            status=status,
            filled_amount=filled_amount,
            remaining_amount=remaining_amount,
            average_price=_to_float(getattr(trade, 'new_price', None), default=0.0) or None,
            reason=getattr(trade, 'error', None) or getattr(trade, 'skip_reason', None),
        )

    def cancel_order(self, order_id: str) -> BrokerOrder:
        response = self._client.cancel_order(order_id)
        if not isinstance(response, dict):
            raise RuntimeError('cancel_order returned invalid response')
        success = bool(response.get('success', False))
        return BrokerOrder(
            order_id=response.get('order_id') or order_id,
            market_id=str(response.get('market_id') or ''),
            side=str(response.get('side') or ''),
            amount=_to_float(response.get('amount')),
            status='cancelled' if success else 'failed',
            filled_amount=_to_float(response.get('filled_amount')),
            remaining_amount=_to_float(response.get('remaining_amount')) or None,
            average_price=_to_float(response.get('average_price')) or None,
            reason=response.get('error') or response.get('message'),
        )

    def fetch_order_status(self, order_id: str) -> BrokerOrder:
        payload = self._client.get_open_orders()
        if not isinstance(payload, dict):
            raise RuntimeError('get_open_orders returned invalid response')
        orders = payload.get('orders')
        if not isinstance(orders, list):
            raise RuntimeError('get_open_orders missing orders')
        for entry in orders:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get('id') or entry.get('order_id')
            if str(entry_id) != order_id:
                continue
            filled_amount = _to_float(entry.get('filled_size') or entry.get('filled_amount') or entry.get('filled'))
            size = _to_float(entry.get('size') or entry.get('amount') or entry.get('shares'))
            remaining_amount = max(size - filled_amount, 0.0) if size else None
            status = self._normalize_open_order_status(entry, filled_amount=filled_amount, remaining_amount=remaining_amount)
            return BrokerOrder(
                order_id=order_id,
                market_id=str(entry.get('market_id') or ''),
                side=str(entry.get('side') or ''),
                amount=size,
                status=status,
                filled_amount=filled_amount,
                remaining_amount=remaining_amount,
                average_price=_to_float(entry.get('avg_price') or entry.get('average_price')) or None,
                reason=entry.get('error') or entry.get('message'),
            )
        return BrokerOrder(
            order_id=order_id,
            market_id='',
            side='',
            amount=0.0,
            status='failed',
            reason='order_not_found',
        )

    @staticmethod
    def _normalize_trade_status(trade, *, filled_amount: float, requested_amount: float) -> str:
        if not getattr(trade, 'success', False):
            return 'rejected'
        order_status = str(getattr(trade, 'order_status', '') or '').lower()
        if order_status == 'matched':
            return 'filled' if filled_amount > 0 else 'failed'
        if requested_amount and 0 < filled_amount < requested_amount:
            return 'partially_filled'
        if order_status in {'live', 'delayed', 'submitted', 'open'}:
            return 'acknowledged'
        if filled_amount > 0:
            return 'partially_filled'
        return 'acknowledged'

    @staticmethod
    def _normalize_open_order_status(entry: dict[str, Any], *, filled_amount: float, remaining_amount: float | None) -> str:
        raw_status = str(entry.get('status') or entry.get('order_status') or '').lower()
        if raw_status in {'cancelled', 'canceled'}:
            return 'cancelled'
        if raw_status in {'rejected'}:
            return 'rejected'
        if raw_status in {'filled', 'matched', 'completed'}:
            return 'filled' if filled_amount > 0 else 'failed'
        if filled_amount > 0 and (remaining_amount is None or remaining_amount > 0):
            return 'partially_filled'
        return 'acknowledged'
