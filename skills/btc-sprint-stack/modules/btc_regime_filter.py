from __future__ import annotations

from datetime import datetime, timezone


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith('Z'):
        value = value.replace('Z', '+00:00')
    return datetime.fromisoformat(value)


def evaluate_regime(context: dict, signal, config: dict) -> dict:
    market = context.get('market', {})
    slippage = context.get('slippage', {})
    warnings = list(context.get('warnings') or [])
    reasons = []

    resolves_at = _parse_time(market.get('resolves_at'))
    minutes_to_resolution = None
    if resolves_at is not None:
        minutes_to_resolution = (resolves_at - datetime.now(timezone.utc)).total_seconds() / 60
        if minutes_to_resolution < 0.5:
            reasons.append(f'resolves_too_soon:{minutes_to_resolution:.2f}m')

    current_probability = market.get('current_probability')
    if current_probability is not None and not (0.1 < float(current_probability) < 0.9):
        reasons.append(f'market_probability_extreme:{float(current_probability):.3f}')

    spread_pct = slippage.get('spread_pct')
    if spread_pct is not None and spread_pct > config['max_slippage_pct']:
        reasons.append(f'spread_too_wide:{spread_pct:.4f}')

    if signal.confidence < config['min_confidence']:
        reasons.append(f'confidence_below_threshold:{signal.confidence:.4f}')

    fee_rate_bps = market.get('fee_rate_bps') or 0
    fee_rate = fee_rate_bps / 10000.0

    if signal.action == 'hold':
        reasons.append('signal_hold')

    approved = not reasons
    return {
        'approved': approved,
        'reasons': reasons,
        'warnings': warnings,
        'minutes_to_resolution': minutes_to_resolution,
        'spread_pct': spread_pct,
        'fee_rate': fee_rate,
    }
