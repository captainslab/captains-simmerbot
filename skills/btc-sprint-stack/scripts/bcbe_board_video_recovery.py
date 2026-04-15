from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES_DIR = ROOT / 'modules'
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

from bcbe_board_video_recovery import run_bounded_recovery  # noqa: E402

DATA_DIR = ROOT / 'data' / 'bcbe_board_video_recovery'
MEETINGS_PATH = DATA_DIR / 'bounded_missing_meetings.json'
VIDEO_CATALOG_PATH = DATA_DIR / 'bounded_video_catalog.json'
VOTE_ITEMS_PATH = DATA_DIR / 'vote_items.jsonl'
VOTE_RECORDS_PATH = DATA_DIR / 'vote_records.jsonl'
MEETING_RESULTS_PATH = DATA_DIR / 'meeting_results.jsonl'
SUMMARY_PATH = DATA_DIR / 'latest_run_summary.json'


def main() -> None:
    parser = argparse.ArgumentParser(description='Run a bounded BCBE board-video recovery pass')
    parser.add_argument('--slice-size', type=int, default=5, help='Maximum number of missing-coverage meetings to attempt')
    parser.add_argument('--meetings-path', type=Path, default=MEETINGS_PATH)
    parser.add_argument('--video-catalog-path', type=Path, default=VIDEO_CATALOG_PATH)
    parser.add_argument('--vote-items-path', type=Path, default=VOTE_ITEMS_PATH)
    parser.add_argument('--vote-records-path', type=Path, default=VOTE_RECORDS_PATH)
    parser.add_argument('--meeting-results-path', type=Path, default=MEETING_RESULTS_PATH)
    parser.add_argument('--summary-path', type=Path, default=SUMMARY_PATH)
    args = parser.parse_args()

    summary = run_bounded_recovery(
        meetings_path=args.meetings_path,
        video_catalog_path=args.video_catalog_path,
        vote_items_path=args.vote_items_path,
        vote_records_path=args.vote_records_path,
        meeting_results_path=args.meeting_results_path,
        summary_path=args.summary_path,
        slice_size=args.slice_size,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
