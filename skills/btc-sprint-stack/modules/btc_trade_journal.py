from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


def ensure_data_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_journal(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def append_journal(path: Path, row: dict) -> None:
    ensure_data_dir(path)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, sort_keys=True) + '\n')


def _rewrite_journal(path: Path, rows: list[dict]) -> None:
    ensure_data_dir(path)
    with path.open('w', encoding='utf-8') as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + '\n')


def _pos_attr(position, name, default=None):
    if isinstance(position, dict):
        return position.get(name, default)
    return getattr(position, name, default)


def reconcile_journal_outcomes(journal_path: Path, rows: list[dict], positions) -> list[dict]:
    """Back-fill pnl_usd and outcome into trade rows whose markets have resolved.

    Matches each unresolved trade row against the positions list by market_id.
    Only updates rows for positions with status='resolved' — open positions are left
    unchanged so mark-to-market noise doesn't corrupt the record.
    Rewrites the journal file only when at least one row changes.
    """
    position_map: dict[str, object] = {}
    for pos in positions:
        mid = _pos_attr(pos, 'market_id')
        if mid:
            position_map[mid] = pos

    updated = False
    reconciled_markets: set[str] = set()
    new_rows: list[dict] = []
    for row in rows:
        if row.get('result_type') == 'trade' and 'pnl_usd' not in row:
            market_id = row.get('market_id')
            pos = position_map.get(market_id) if market_id else None
            if pos is not None and _pos_attr(pos, 'status') == 'resolved':
                row = dict(row)
                if market_id not in reconciled_markets:
                    pnl = float(_pos_attr(pos, 'pnl') or 0.0)
                    row['pnl_usd'] = pnl
                    row['outcome'] = 'win' if pnl > 0 else 'loss'
                    reconciled_markets.add(market_id)
                else:
                    row['pnl_usd'] = 0.0
                    row['outcome'] = 'hedged'
                updated = True
        new_rows.append(row)

    if updated:
        _rewrite_journal(journal_path, new_rows)

    return new_rows
