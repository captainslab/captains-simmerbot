from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
SCRIPTS = ROOT / 'skills' / 'btc-sprint-stack' / 'scripts'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from btc_analyst import analyze_session  # noqa: E402
from btc_review_metrics import build_review_metrics  # noqa: E402
import analyze_sprints  # noqa: E402


def _accepted_row(index: int, *, edge: float, confidence: float, outcome: str | None = None, pnl_usd: float | None = None) -> dict:
    row = {
        'ts': f'2026-04-09T00:{index:02d}:00+00:00',
        'decision': 'candidate',
        'result_type': 'trade',
        'execution_status': 'accepted',
        'signal_data': {
            'edge': edge,
            'confidence': confidence,
            'signal_source': 'unit-test',
        },
    }
    if outcome is not None:
        row['outcome'] = outcome
    if pnl_usd is not None:
        row['pnl_usd'] = pnl_usd
    return row


def test_build_review_metrics_reports_requested_fields():
    config = {'min_edge': 0.07, 'min_confidence': 0.65}
    journal_rows = [
        _accepted_row(index, edge=0.11, confidence=0.75, outcome='win', pnl_usd=1.0)
        for index in range(10)
    ]

    report = build_review_metrics(journal_rows, config)

    assert report == {
        'rolling_10_trade_win_loss': '10-0',
        'average_edge_and_confidence_of_accepted_trades': 'edge=0.1100, confidence=0.7500 (10 accepted trades)',
        'accepted_trade_clustering': 'well above (0/10 within +0.015 edge and +0.03 confidence bands)',
        'first_sign_of_negative_drift': 'none',
        'next_adjustment_trigger': 'first completed 10-trade resolved window at 4-6 or worse',
    }


def test_build_review_metrics_marks_early_negative_drift_and_unavailable_rolling_window():
    config = {'min_edge': 0.07, 'min_confidence': 0.65}
    journal_rows = [
        _accepted_row(0, edge=0.072, confidence=0.66, outcome='win', pnl_usd=1.0),
        _accepted_row(1, edge=0.074, confidence=0.665, outcome='win', pnl_usd=1.0),
        _accepted_row(2, edge=0.073, confidence=0.662, outcome='loss', pnl_usd=-1.0),
        _accepted_row(3, edge=0.075, confidence=0.668, outcome='loss', pnl_usd=-1.0),
        _accepted_row(4, edge=0.071, confidence=0.659, outcome='loss', pnl_usd=-1.0),
    ]

    report = build_review_metrics(journal_rows, config)

    assert report['rolling_10_trade_win_loss'] == 'unavailable (5/10 resolved accepted trades)'
    assert report['accepted_trade_clustering'] == 'near threshold (5/5 within +0.015 edge and +0.03 confidence bands)'
    assert report['first_sign_of_negative_drift'] == '2026-04-09T00:04:00+00:00: 2-3 over 5 resolved accepted trades, net pnl=-1.0000'


def test_analyze_session_returns_only_five_numbered_lines(tmp_path):
    data_dir = tmp_path / 'data'
    config_dir = tmp_path / 'config'
    data_dir.mkdir()
    config_dir.mkdir()
    (config_dir / 'defaults.json').write_text(json.dumps({'min_edge': 0.07, 'min_confidence': 0.65}))
    rows = [
        _accepted_row(index, edge=0.11, confidence=0.75, outcome='win', pnl_usd=1.0)
        for index in range(10)
    ]
    (data_dir / 'journal.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\n')

    review = analyze_session(data_dir)

    assert review.splitlines() == [
        '1. rolling 10-trade win/loss once available: 10-0',
        '2. average edge and confidence of accepted trades: edge=0.1100, confidence=0.7500 (10 accepted trades)',
        '3. whether accepted trades cluster near threshold or well above: well above (0/10 within +0.015 edge and +0.03 confidence bands)',
        '4. first sign of negative drift (if any): none',
        '5. single condition to trigger next adjustment: first completed 10-trade resolved window at 4-6 or worse',
    ]


def test_analyze_sprints_build_review_report_only_returns_requested_fields(tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    config_dir = tmp_path / 'config'
    data_dir.mkdir()
    config_dir.mkdir()
    (data_dir / 'journal.jsonl').write_text(json.dumps(_accepted_row(0, edge=0.11, confidence=0.75, outcome='win', pnl_usd=1.0)) + '\n')
    (data_dir / 'live_params.json').write_text(json.dumps({}))
    (config_dir / 'defaults.json').write_text(json.dumps({'min_edge': 0.07, 'min_confidence': 0.65}))

    monkeypatch.setattr(analyze_sprints, 'JOURNAL_PATH', data_dir / 'journal.jsonl')
    monkeypatch.setattr(analyze_sprints, 'LIVE_PARAMS_PATH', data_dir / 'live_params.json')
    monkeypatch.setattr(analyze_sprints, 'DEFAULTS_PATH', config_dir / 'defaults.json')

    report = analyze_sprints.build_review_report()

    assert set(report) == {
        'rolling_10_trade_win_loss',
        'average_edge_and_confidence_of_accepted_trades',
        'accepted_trade_clustering',
        'first_sign_of_negative_drift',
        'next_adjustment_trigger',
    }
