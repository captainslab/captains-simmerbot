from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))

from bcbe_board_video_recovery import (  # noqa: E402
    build_vote_records,
    extract_vote_items,
    match_meeting_to_video,
    run_bounded_recovery,
)


class DummyTranscriptClient:
    def __init__(self, mapping: dict[str, object]):
        self.mapping = mapping

    def fetch(self, video_id: str):
        payload = self.mapping[video_id]
        if isinstance(payload, Exception):
            raise payload
        return payload


def test_match_meeting_to_video_requires_date_and_meeting_type():
    meeting = {
        'meeting_date': '2024-07-18',
        'meeting_title': 'BCBE Board Meeting',
        'meeting_type': 'board_meeting',
    }
    videos = [
        {
            'video_url': 'https://www.youtube.com/watch?v=xzL5NL9mK3I',
            'title': 'BCBE Board Meetings: July 18, 2024',
            'meeting_date': '2024-07-18',
            'meeting_type': 'board_meeting',
            'normalized_title': 'bcbe board meetings july 18 2024',
        },
        {
            'video_url': 'https://www.youtube.com/watch?v=dXNFrv8j-fI',
            'title': 'BCBE Special Board Meeting April 07 2026',
            'meeting_date': '2024-07-18',
            'meeting_type': 'special_board_meeting',
            'normalized_title': 'bcbe special board meeting april 07 2026',
        },
    ]

    matched = match_meeting_to_video(meeting, videos)

    assert matched is not None
    assert matched['video_url'] == 'https://www.youtube.com/watch?v=xzL5NL9mK3I'


def test_extract_vote_items_keeps_vote_bearing_language_only():
    meeting = {
        'meeting_date': '2024-07-18',
        'meeting_title': 'BCBE Board Meeting',
        'meeting_type': 'board_meeting',
    }
    video = {
        'video_url': 'https://www.youtube.com/watch?v=xzL5NL9mK3I',
        'video_id': 'xzL5NL9mK3I',
        'title': 'BCBE Board Meetings: July 18, 2024',
        'author_url': 'https://www.youtube.com/@bcbeboardvideos9344',
    }
    transcript_rows = [
        {'text': 'Board members discussed the monthly report.'},
        {'text': 'There is a motion to approve the consent agenda.'},
        {'text': 'The motion passed unanimously.'},
        {'text': 'Roll call vote: yes Smith, yes Brown, no Jones, abstain Green.'},
    ]

    items = extract_vote_items(meeting, video, transcript_rows)
    vote_types = {item['vote_item_type'] for item in items}
    excerpts = {item['excerpt'] for item in items}

    assert vote_types == {'motion', 'outcome', 'unanimous_approval', 'named_vote'}
    assert all('monthly report' not in excerpt.lower() for excerpt in excerpts)

    records = build_vote_records(items)
    assert len(records) == len(items)
    assert {record['vote_record_type'] for record in records} == vote_types


def test_run_bounded_recovery_persists_summary_and_precise_miss_classes(tmp_path):
    meetings_path = tmp_path / 'meetings.json'
    video_catalog_path = tmp_path / 'catalog.json'
    vote_items_path = tmp_path / 'vote_items.jsonl'
    vote_records_path = tmp_path / 'vote_records.jsonl'
    meeting_results_path = tmp_path / 'meeting_results.jsonl'
    summary_path = tmp_path / 'summary.json'

    meetings_path.write_text(
        json.dumps(
            [
                {
                    'meeting_date': '2024-07-18',
                    'meeting_title': 'BCBE Board Meeting',
                    'extracted_coverage_status': 'missing',
                    'minutes_status': 'unreachable',
                },
                {
                    'meeting_date': '2024-05-16',
                    'meeting_title': 'BCBE Board Meeting',
                    'extracted_coverage_status': 'missing',
                    'minutes_status': 'unreachable',
                },
            ]
        )
    )
    video_catalog_path.write_text(
        json.dumps(
            [
                {
                    'video_url': 'https://www.youtube.com/watch?v=xzL5NL9mK3I',
                    'title': 'BCBE Board Meetings: July 18, 2024',
                    'author_name': 'BCBE Board Videos',
                    'author_url': 'https://www.youtube.com/@bcbeboardvideos9344',
                }
            ]
        )
    )

    class DummyNoTranscriptError(Exception):
        pass

    summary = run_bounded_recovery(
        meetings_path=meetings_path,
        video_catalog_path=video_catalog_path,
        vote_items_path=vote_items_path,
        vote_records_path=vote_records_path,
        meeting_results_path=meeting_results_path,
        summary_path=summary_path,
        slice_size=5,
        transcript_client=DummyTranscriptClient({'xzL5NL9mK3I': DummyNoTranscriptError('blocked')}),
    )

    assert summary['meetings_attempted'] == 2
    assert summary['meetings_completed'] == 0
    assert summary['meetings_failed'] == 2
    assert summary['vote_items_before'] == 0
    assert summary['vote_items_after'] == 0
    assert summary['vote_records_before'] == 0
    assert summary['vote_records_after'] == 0
    assert summary['failure_counts_by_class']['no_matching_video'] == 1
    assert summary['failure_counts_by_class']['other'] == 1

    meeting_results = [json.loads(line) for line in meeting_results_path.read_text().splitlines() if line.strip()]
    assert {row['miss_class'] for row in meeting_results} == {'no_matching_video', 'other'}
    assert summary_path.exists()
