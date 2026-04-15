from __future__ import annotations

from statistics import mean
from typing import Any


_ACCEPTED_RESULT_TYPES = {'trade', 'dry_run'}
_RESOLVED_OUTCOMES = {'win', 'loss'}
_EDGE_NEAR_BAND = 0.015
_CONFIDENCE_NEAR_BAND = 0.03


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ''):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _trade_ts(row: dict[str, Any]) -> str:
    return str(row.get('ts') or row.get('timestamp') or row.get('cycle_timestamp') or 'unknown')


def _signal_edge(row: dict[str, Any]) -> float | None:
    signal_data = row.get('signal_data') or {}
    if isinstance(signal_data, dict):
        edge = _to_float(signal_data.get('edge'))
        if edge is not None:
            return edge
    return _to_float(row.get('signal_edge') if row.get('signal_edge') is not None else row.get('edge'))


def _signal_confidence(row: dict[str, Any]) -> float | None:
    signal_data = row.get('signal_data') or {}
    if isinstance(signal_data, dict):
        confidence = _to_float(signal_data.get('confidence'))
        if confidence is not None:
            return confidence
    value = row.get('signal_confidence') if row.get('signal_confidence') is not None else row.get('confidence')
    return _to_float(value)


def _pnl_value(row: dict[str, Any]) -> float | None:
    return _to_float(row.get('pnl_usd'))


def _trade_outcome(row: dict[str, Any]) -> str | None:
    outcome = row.get('outcome')
    if outcome in _RESOLVED_OUTCOMES:
        return str(outcome)
    pnl = _pnl_value(row)
    if pnl is None or pnl == 0:
        return None
    return 'win' if pnl > 0 else 'loss'


def accepted_trade_rows(journal_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in journal_rows
        if row.get('decision') == 'candidate'
        and row.get('result_type') in _ACCEPTED_RESULT_TYPES
        and row.get('execution_status', 'accepted') == 'accepted'
    ]


def resolved_trade_rows(journal_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in accepted_trade_rows(journal_rows):
        if _trade_outcome(row) in _RESOLVED_OUTCOMES:
            rows.append(row)
    return rows


def build_review_metrics(journal_rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, str]:
    accepted = accepted_trade_rows(journal_rows)
    resolved = resolved_trade_rows(journal_rows)
    min_edge = _to_float(config.get('min_edge')) or 0.0
    min_confidence = _to_float(config.get('min_confidence')) or 0.0

    accepted_edges = [_signal_edge(row) for row in accepted]
    accepted_edges = [value for value in accepted_edges if value is not None]
    accepted_confidences = [_signal_confidence(row) for row in accepted]
    accepted_confidences = [value for value in accepted_confidences if value is not None]

    if len(resolved) < 10:
        rolling_10 = f'unavailable ({len(resolved)}/10 resolved accepted trades)'
    else:
        last_ten = resolved[-10:]
        wins = sum(1 for row in last_ten if _trade_outcome(row) == 'win')
        losses = sum(1 for row in last_ten if _trade_outcome(row) == 'loss')
        rolling_10 = f'{wins}-{losses}'

    if accepted_edges and accepted_confidences:
        avg_edge = mean(accepted_edges)
        avg_confidence = mean(accepted_confidences)
        average_quality = (
            f'edge={avg_edge:.4f}, confidence={avg_confidence:.4f} '
            f'({len(accepted)} accepted trades)'
        )
    else:
        average_quality = 'unavailable (no accepted trades yet)'

    thresholdable = [
        row for row in accepted
        if _signal_edge(row) is not None and _signal_confidence(row) is not None
    ]
    if not thresholdable:
        clustering = 'unavailable (no accepted trades with edge/confidence yet)'
    else:
        near_threshold_count = sum(
            1
            for row in thresholdable
            if (_signal_edge(row) or 0.0) <= min_edge + _EDGE_NEAR_BAND
            and (_signal_confidence(row) or 0.0) <= min_confidence + _CONFIDENCE_NEAR_BAND
        )
        ratio = near_threshold_count / len(thresholdable)
        avg_edge = mean((_signal_edge(row) or 0.0) for row in thresholdable)
        avg_confidence = mean((_signal_confidence(row) or 0.0) for row in thresholdable)
        if ratio >= 0.6:
            label = 'near threshold'
        elif ratio <= 0.2 and avg_edge >= min_edge + _EDGE_NEAR_BAND and avg_confidence >= min_confidence + _CONFIDENCE_NEAR_BAND:
            label = 'well above'
        else:
            label = 'mixed'
        clustering = (
            f'{label} ({near_threshold_count}/{len(thresholdable)} within '
            f'+{_EDGE_NEAR_BAND:.3f} edge and +{_CONFIDENCE_NEAR_BAND:.2f} confidence bands)'
        )

    negative_drift = 'none'
    for index in range(4, len(resolved)):
        window = resolved[index - 4:index + 1]
        wins = sum(1 for row in window if _trade_outcome(row) == 'win')
        losses = sum(1 for row in window if _trade_outcome(row) == 'loss')
        pnls = [_pnl_value(row) for row in window]
        numeric_pnls = [value for value in pnls if value is not None]
        net_pnl = sum(numeric_pnls) if len(numeric_pnls) == len(window) else None
        if losses > wins or (net_pnl is not None and net_pnl < 0):
            details = f'{wins}-{losses} over 5 resolved accepted trades'
            if net_pnl is not None:
                details += f', net pnl={net_pnl:.4f}'
            negative_drift = f'{_trade_ts(window[-1])}: {details}'
            break

    return {
        'rolling_10_trade_win_loss': rolling_10,
        'average_edge_and_confidence_of_accepted_trades': average_quality,
        'accepted_trade_clustering': clustering,
        'first_sign_of_negative_drift': negative_drift,
        'next_adjustment_trigger': 'first completed 10-trade resolved window at 4-6 or worse',
    }


def format_review_metrics(metrics: dict[str, str]) -> str:
    return '\n'.join(
        [
            f"1. rolling 10-trade win/loss once available: {metrics['rolling_10_trade_win_loss']}",
            f"2. average edge and confidence of accepted trades: {metrics['average_edge_and_confidence_of_accepted_trades']}",
            f"3. whether accepted trades cluster near threshold or well above: {metrics['accepted_trade_clustering']}",
            f"4. first sign of negative drift (if any): {metrics['first_sign_of_negative_drift']}",
            f"5. single condition to trigger next adjustment: {metrics['next_adjustment_trigger']}",
        ]
    )
