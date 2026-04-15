from __future__ import annotations

import math

POLYMARKET_MIN_SHARES = 5


def build_reasoning(signal, regime, trade_amount: float, llm_reasoning: str | None = None) -> str:
    regime_bits = ', '.join(regime['warnings']) if regime['warnings'] else 'no warnings'
    base_reasoning = llm_reasoning or signal.reasoning
    return (
        f"{base_reasoning} Edge={signal.edge:.4f}, confidence={signal.confidence:.4f}, "
        f"trade_amount=${trade_amount:.2f}, regime={regime_bits}."
    )


def _side_price(side: str, context: dict | None) -> float | None:
    """Return expected execution price from market context's current_probability."""
    if not context:
        return None
    prob = (context.get('market') or {}).get('current_probability')
    if prob is None or not (0.01 < prob < 0.99):
        return None
    raw = float(prob) if side == 'yes' else (1.0 - float(prob))
    return round(raw, 2)


def _preflight_block_reason(preflight) -> str | None:
    if preflight is None:
        return None

    if isinstance(preflight, dict):
        data = preflight
    else:
        data = getattr(preflight, '__dict__', {})
        if not isinstance(data, dict):
            data = {}

    for key in ('error', 'skip_reason', 'reason'):
        value = data.get(key)
        if value:
            return str(value)

    status = str(data.get('status') or data.get('order_status') or '').lower()
    if status in {'rejected', 'failed', 'cancelled', 'canceled'}:
        return status

    if data.get('success') is False:
        return 'prepare_real_trade_rejected'

    return None


def execute_trade(
    client,
    market_id: str,
    side: str,
    amount: float,
    signal,
    regime: dict,
    *,
    live: bool,
    source: str,
    skill_slug: str,
    venue: str,
    validate_real_path: bool,
    llm_decision: dict | None = None,
    context: dict | None = None,
    provider_name: str | None = None,
    model_name: str | None = None,
) -> dict:
    llm_reasoning = None
    if isinstance(llm_decision, dict):
        llm_reasoning = llm_decision.get('reasoning')

    pre_submit_guard: dict | None = None

    if live and venue == 'polymarket':
        price = _side_price(side, context)
        if price is None:
            return {
                'live': live,
                'venue': venue,
                'market_id': market_id,
                'side': side,
                'amount': amount,
                'reasoning': build_reasoning(signal, regime, amount, llm_reasoning=llm_reasoning),
                'source': source,
                'skill_slug': skill_slug,
                'decision_source': 'llm',
                'signal_data': signal.to_signal_data(),
                'result_type': 'dry_run',
                'pre_submit_guard': {'guard_skipped': True, 'reason': 'current_probability_unavailable'},
                'blocked': True,
                'block_reason': 'cannot_verify_minimum_shares:current_probability_unavailable',
            }
        if price is not None:
            expected_shares = amount / price
            if expected_shares < POLYMARKET_MIN_SHARES:
                # Bump amount to clear minimum with a 2% buffer
                amount = math.ceil(POLYMARKET_MIN_SHARES * price * 1.02 * 100) / 100
                expected_shares = amount / price
            pre_submit_guard = {
                'side': side,
                'intended_price': round(price, 4),
                'requested_cost': round(amount, 2),
                'expected_shares': round(expected_shares, 2),
                'rounded_shares': math.floor(expected_shares * 100) / 100,
                'order_type': 'GTC',
                'provider': provider_name,
                'model': model_name,
            }

    reasoning = build_reasoning(signal, regime, amount, llm_reasoning=llm_reasoning)
    signal_data = signal.to_signal_data()
    result = {
        'live': live,
        'venue': venue,
        'market_id': market_id,
        'side': side,
        'amount': amount,
        'reasoning': reasoning,
        'source': source,
        'skill_slug': skill_slug,
        'decision_source': 'llm',
        'signal_data': signal_data,
    }
    if pre_submit_guard is not None:
        result['pre_submit_guard'] = pre_submit_guard

    if live and venue == 'polymarket' and validate_real_path:
        preflight = client.prepare_real_trade(market_id, side, amount)
        preflight_data = getattr(preflight, '__dict__', preflight)
        result['preflight'] = preflight_data
        block_reason = _preflight_block_reason(preflight)
        if block_reason is not None:
            result['result_type'] = 'dry_run'
            result['blocked'] = True
            result['block_reason'] = block_reason
            return result

    if live:
        trade = client.trade(
            market_id=market_id,
            side=side,
            amount=amount,
            venue=venue,
            reasoning=reasoning,
            source=source,
            skill_slug=skill_slug,
            signal_data=signal_data,
            order_type='GTC',
        )
        result['result_type'] = 'trade'
        result['trade'] = getattr(trade, '__dict__', trade)
        return result

    result['result_type'] = 'dry_run'
    if validate_real_path and venue == 'polymarket':
        preflight = client.prepare_real_trade(market_id, side, amount)
        result['preflight'] = getattr(preflight, '__dict__', preflight)
    return result
