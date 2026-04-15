from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'data'
JOURNAL_PATH = DATA_DIR / 'journal.jsonl'
LLM_DECISIONS_PATH = DATA_DIR / 'llm_decisions.jsonl'
LIVE_PARAMS_PATH = DATA_DIR / 'live_params.json'
PENDING_RULES_PATH = DATA_DIR / 'pending_rules.json'
DEFAULTS_PATH = ROOT / 'config' / 'defaults.json'
MODULES_DIR = ROOT / 'modules'
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

from btc_review_metrics import build_review_metrics  # noqa: E402


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def build_review_report() -> dict[str, Any]:
    journal_rows = _read_jsonl(JOURNAL_PATH)
    live_params = _read_json(LIVE_PARAMS_PATH, {})
    defaults = _read_json(DEFAULTS_PATH, {})
    effective_config = dict(defaults)
    if isinstance(live_params, dict):
        effective_config.update(live_params)
    return build_review_metrics(journal_rows, effective_config)


def main() -> None:
    parser = argparse.ArgumentParser(description='Summarize BTC sprint logs and LLM decisions')
    parser.add_argument('--review', action='store_true', help='Print the review report')
    args = parser.parse_args()

    report = build_review_report()
    if args.review:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == '__main__':
    main()
