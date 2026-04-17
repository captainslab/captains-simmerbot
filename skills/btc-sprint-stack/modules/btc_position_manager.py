from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _safe_get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _position_sources(position) -> list[str]:
    sources = _safe_get(position, 'sources', None)
    if isinstance(sources, str):
        return [sources] if sources else []
    if isinstance(sources, (list, tuple, set)):
        return [str(source) for source in sources if str(source)]

    source = _safe_get(position, 'source', None)
    if source:
        return [str(source)]
    return []


def _position_has_shares(position) -> bool:
    for field in ('shares', 'shares_yes', 'shares_no'):
        value = _safe_get(position, field, 0.0)
        try:
            if float(value or 0.0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def active_positions_for_skill(positions, skill_slug: str) -> list:
    scoped = []
    for position in positions:
        sources = _position_sources(position)
        if sources and skill_slug not in sources:
            continue
        if not _position_has_shares(position):
            continue
        scoped.append(position)
    return scoped


def _parse_trade_ts(raw_ts: str | None) -> datetime | None:
    if not raw_ts:
        return None
    try:
        return datetime.fromisoformat(str(raw_ts).replace('Z', '+00:00'))
    except ValueError:
        return None


def enforce_risk_limits(
    settings: dict,
    positions,
    config: dict,
    skill_slug: str,
    journal_rows: list[dict],
    *,
    execution_mode: str = 'dry_run',
    regime: dict | None = None,
) -> dict:
    reasons = []
    settings = settings or {}
    scoped_positions = active_positions_for_skill(positions, skill_slug)
    if execution_mode not in {'dry_run', 'live'}:
        reasons.append(f'unknown_execution_mode:{execution_mode}')

    if len(scoped_positions) >= config['max_open_positions']:
        reasons.append('max_open_positions_reached')

    if settings.get('trading_paused'):
        reasons.append('trading_paused')

    today_utc = datetime.now(timezone.utc).date().isoformat()
    daily_spent = sum(
        float((row.get('risk_state') or {}).get('trade_amount_usd') or 0)
        for row in journal_rows
        if row.get('result_type') == 'trade'
        and (row.get('ts') or '')[:10] == today_utc
    )
    if daily_spent >= config['max_daily_loss_usd']:
        reasons.append('max_daily_loss_reached')

    now = datetime.now(timezone.utc)
    hourly_cutoff = now - timedelta(hours=1)
    recent_trades = []
    for row in journal_rows:
        if row.get('result_type') != 'trade':
            continue
        trade_time = _parse_trade_ts(row.get('ts'))
        if trade_time is not None and trade_time >= hourly_cutoff:
            recent_trades.append(row)
    if len(recent_trades) >= config['max_trades_per_hour']:
        reasons.append('max_trades_per_hour_reached')

    recent_losses = [
        (_parse_trade_ts(row.get('ts')), row)
        for row in journal_rows
        if row.get('pnl_usd', 0) < 0
    ]
    recent_losses = [(loss_time, row) for loss_time, row in recent_losses if loss_time is not None]
    if recent_losses:
        loss_time, _ = max(recent_losses, key=lambda item: item[0])
        cooldown = (now - loss_time).total_seconds() / 60
        if cooldown < config['cooldown_after_loss_minutes']:
            reasons.append(f'cooldown_active:{cooldown:.1f}m')

    if regime is not None:
        spread_pct = regime.get('spread_pct')
        if spread_pct is not None and spread_pct > config['max_slippage_pct']:
            reasons.append(f'max_slippage_pct_exceeded:{spread_pct:.4f}')

    amount = min(
        config['max_trade_usd'],
        config['max_single_market_exposure_usd'],
        config['bankroll_usd'],
    )
    amount = round(max(amount, 0.0), 2)
    if amount <= 0:
        reasons.append('non_positive_trade_amount')

    return {
        'allowed': not reasons,
        'reasons': reasons,
        'trade_amount_usd': amount,
        'open_positions': len(scoped_positions),
        'daily_spent': daily_spent,
        'execution_mode': execution_mode,
    }
