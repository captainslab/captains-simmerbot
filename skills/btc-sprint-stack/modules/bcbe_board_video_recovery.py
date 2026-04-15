from __future__ import annotations

import json
import re
from hashlib import sha1
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from youtube_transcript_api import YouTubeTranscriptApi, _errors as transcript_errors

OFFICIAL_AUTHOR_NAME = 'BCBE Board Videos'
OFFICIAL_CHANNEL_URL = 'https://www.youtube.com/@bcbeboardvideos9344'
PROVENANCE_MARKER = 'video/transcript-derived'

_MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}


class TranscriptClient(Protocol):
    def fetch(self, video_id: str) -> list[dict[str, Any]]:
        ...


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text('')
        return
    path.write_text('\n'.join(json.dumps(row, sort_keys=True) for row in rows) + '\n')


def _normalize_text(value: str | None) -> str:
    text = (value or '').lower()
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return ' '.join(text.split())


def _meeting_type(value: str | None) -> str:
    normalized = _normalize_text(value)
    if 'special board meeting' in normalized:
        return 'special_board_meeting'
    if 'work session' in normalized:
        return 'board_work_session'
    if 'board meeting' in normalized or 'board meetings' in normalized:
        return 'board_meeting'
    return 'unknown'


def _parse_date_from_text(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', value)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    day = int(match.group(2))
    year = int(match.group(3))
    return f'{year:04d}-{month:02d}-{day:02d}'


def _stable_id(prefix: str, *parts: str) -> str:
    joined = '||'.join(parts)
    return f'{prefix}_{sha1(joined.encode("utf-8")).hexdigest()[:16]}'


def video_id_from_url(video_url: str) -> str | None:
    parsed = urlparse(video_url)
    if parsed.netloc in {'youtu.be', 'www.youtu.be'}:
        candidate = parsed.path.strip('/')
        return candidate or None
    query_video_id = parse_qs(parsed.query).get('v')
    if query_video_id:
        return query_video_id[0]
    parts = [part for part in parsed.path.split('/') if part]
    if 'embed' in parts:
        embed_index = parts.index('embed')
        if len(parts) > embed_index + 1:
            return parts[embed_index + 1]
    return None


def fetch_oembed(video_url: str, session: requests.sessions.Session | None = None) -> dict[str, Any]:
    http = session or requests.Session()
    query = urlencode({'url': video_url, 'format': 'json'})
    response = http.get(
        f'https://www.youtube.com/oembed?{query}',
        headers={'User-Agent': 'Mozilla/5.0'},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    payload['video_url'] = video_url
    payload['video_id'] = video_id_from_url(video_url)
    payload['meeting_date'] = _parse_date_from_text(str(payload.get('title') or ''))
    payload['meeting_type'] = _meeting_type(str(payload.get('title') or ''))
    payload['normalized_title'] = _normalize_text(str(payload.get('title') or ''))
    return payload


def load_video_catalog(path: Path, session: requests.sessions.Session | None = None) -> list[dict[str, Any]]:
    catalog = _read_json(path, [])
    videos: list[dict[str, Any]] = []
    for entry in catalog:
        video_url = str(entry.get('video_url') or '').strip()
        if not video_url:
            continue
        enriched = dict(entry)
        if not enriched.get('title') or not enriched.get('author_name'):
            enriched.update(fetch_oembed(video_url, session=session))
        if enriched.get('author_name') != OFFICIAL_AUTHOR_NAME:
            continue
        enriched['video_url'] = video_url
        enriched['video_id'] = enriched.get('video_id') or video_id_from_url(video_url)
        enriched['meeting_date'] = enriched.get('meeting_date') or _parse_date_from_text(str(enriched.get('title') or ''))
        enriched['meeting_type'] = enriched.get('meeting_type') or _meeting_type(str(enriched.get('title') or ''))
        enriched['normalized_title'] = _normalize_text(str(enriched.get('title') or ''))
        videos.append(enriched)
    return videos


def match_meeting_to_video(meeting: dict[str, Any], videos: list[dict[str, Any]]) -> dict[str, Any] | None:
    meeting_date = str(meeting.get('meeting_date') or '')
    meeting_title = str(meeting.get('meeting_title') or '')
    meeting_type = str(meeting.get('meeting_type') or _meeting_type(meeting_title))
    meeting_tokens = set(_normalize_text(meeting_title).split())

    best_score = -1
    best_match: dict[str, Any] | None = None
    for video in videos:
        if str(video.get('meeting_date') or '') != meeting_date:
            continue
        video_type = str(video.get('meeting_type') or 'unknown')
        if meeting_type != 'unknown' and video_type not in {meeting_type, 'unknown'}:
            continue
        score = 100
        if meeting_type == video_type:
            score += 25
        title_tokens = set(str(video.get('normalized_title') or '').split())
        score += len(meeting_tokens & title_tokens)
        if score > best_score:
            best_score = score
            best_match = video
    return best_match


class YouTubeTranscriptClient:
    def __init__(self) -> None:
        self._api = YouTubeTranscriptApi()

    def fetch(self, video_id: str) -> list[dict[str, Any]]:
        fetched = self._api.fetch(video_id, languages=['en'])
        rows: list[dict[str, Any]] = []
        for row in fetched:
            rows.append(
                {
                    'text': getattr(row, 'text', None),
                    'start': getattr(row, 'start', None),
                    'duration': getattr(row, 'duration', None),
                }
            )
        return rows


def _clean_excerpt(value: str | None) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text


def fetch_transcript_rows(video_id: str, transcript_client: TranscriptClient | None = None) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    client = transcript_client or YouTubeTranscriptClient()
    try:
        rows = client.fetch(video_id)
    except (
        transcript_errors.RequestBlocked,
        transcript_errors.IpBlocked,
        transcript_errors.NoTranscriptFound,
        transcript_errors.TranscriptsDisabled,
        transcript_errors.PoTokenRequired,
    ) as exc:
        return None, 'no_transcript', str(exc)
    except transcript_errors.YouTubeDataUnparsable as exc:
        return None, 'transcript_unusable', str(exc)
    except (
        transcript_errors.VideoUnavailable,
        transcript_errors.VideoUnplayable,
        transcript_errors.YouTubeRequestFailed,
        transcript_errors.CouldNotRetrieveTranscript,
    ) as exc:
        return None, 'other', str(exc)
    except Exception as exc:  # pragma: no cover - last-resort classification
        return None, 'other', f'{type(exc).__name__}: {exc}'

    cleaned_rows = []
    for row in rows:
        excerpt = _clean_excerpt(row.get('text'))
        if excerpt:
            cleaned_rows.append(
                {
                    'text': excerpt,
                    'start': row.get('start'),
                    'duration': row.get('duration'),
                }
            )
    if not cleaned_rows:
        return None, 'transcript_unusable', 'transcript returned no usable text rows'
    return cleaned_rows, None, None


def _excerpt_vote_types(excerpt: str) -> list[str]:
    normalized = _normalize_text(excerpt)
    vote_types: list[str] = []
    if re.search(r'\b(aye|ayes|nay|nays|abstain|abstained)\b', normalized) or (
        'vote' in normalized and re.search(r'\b(yes|no)\b', normalized)
    ):
        vote_types.append('named_vote')
    if re.search(r'\b(unanimous|unanimously|without objection)\b', normalized) or 'all in favor' in normalized:
        vote_types.append('unanimous_approval')
    if re.search(r'\b(passed|failed|approved|denied|carried|carries|adopted)\b', normalized):
        vote_types.append('outcome')
    if re.search(r'\b(motion|moved to|move to|moves to|motion to approve|second)\b', normalized):
        vote_types.append('motion')
    return vote_types


def extract_vote_items(
    meeting: dict[str, Any],
    video: dict[str, Any],
    transcript_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    excerpts: list[str] = []
    for index, row in enumerate(transcript_rows):
        excerpts.append(row['text'])
        if index + 1 < len(transcript_rows) and _excerpt_vote_types(row['text']):
            excerpts.append(f"{row['text']} {transcript_rows[index + 1]['text']}")

    items_by_id: dict[str, dict[str, Any]] = {}
    for excerpt in excerpts:
        cleaned = _clean_excerpt(excerpt)
        if not cleaned:
            continue
        for vote_type in _excerpt_vote_types(cleaned):
            item = {
                'id': _stable_id(
                    'vote_item',
                    str(meeting.get('meeting_date') or ''),
                    str(meeting.get('meeting_title') or ''),
                    str(video.get('video_url') or ''),
                    vote_type,
                    cleaned,
                ),
                'meeting_date': meeting.get('meeting_date'),
                'meeting_title': meeting.get('meeting_title'),
                'meeting_type': meeting.get('meeting_type') or _meeting_type(str(meeting.get('meeting_title') or '')),
                'vote_item_type': vote_type,
                'excerpt': cleaned,
                'provenance': PROVENANCE_MARKER,
                'source_type': 'video_transcript',
                'source_platform': 'youtube',
                'source_channel_url': video.get('author_url') or OFFICIAL_CHANNEL_URL,
                'source_video_url': video.get('video_url'),
                'source_video_title': video.get('title'),
                'source_video_id': video.get('video_id'),
            }
            items_by_id[item['id']] = item
    return list(items_by_id.values())


def build_vote_records(vote_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in vote_items:
        record = {
            'id': _stable_id(
                'vote_record',
                str(item.get('meeting_date') or ''),
                str(item.get('meeting_title') or ''),
                str(item.get('vote_item_type') or ''),
                str(item.get('excerpt') or ''),
            ),
            'meeting_date': item.get('meeting_date'),
            'meeting_title': item.get('meeting_title'),
            'meeting_type': item.get('meeting_type'),
            'vote_record_type': item.get('vote_item_type'),
            'source_excerpt': item.get('excerpt'),
            'provenance': item.get('provenance'),
            'source_type': item.get('source_type'),
            'source_platform': item.get('source_platform'),
            'source_channel_url': item.get('source_channel_url'),
            'source_video_url': item.get('source_video_url'),
            'source_video_title': item.get('source_video_title'),
            'source_video_id': item.get('source_video_id'),
        }
        records.append(record)
    return records


def _upsert(existing_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(row.get('id')): row for row in existing_rows}
    for row in new_rows:
        by_id[str(row.get('id'))] = row
    return list(by_id.values())


def summarize_verdict(summary: dict[str, Any]) -> str:
    recovered = int(summary['vote_records_after']) - int(summary['vote_records_before'])
    matched = len(summary['matched_video_urls'])
    no_transcript = int(summary['failure_counts_by_class'].get('no_transcript', 0))
    if recovered > 0:
        return 'worth continuing for meetings where the official YouTube upload exposes a reachable transcript'
    if matched > 0 and no_transcript == matched:
        return 'not worth continuing from this runtime until official YouTube transcript access is reachable'
    if matched == 0:
        return 'not worth continuing until a better bounded official video catalog is available'
    return 'worth continuing only if transcript reachability improves'


def run_bounded_recovery(
    *,
    meetings_path: Path,
    video_catalog_path: Path,
    vote_items_path: Path,
    vote_records_path: Path,
    meeting_results_path: Path,
    summary_path: Path,
    slice_size: int,
    transcript_client: TranscriptClient | None = None,
    session: requests.sessions.Session | None = None,
) -> dict[str, Any]:
    meetings = _read_json(meetings_path, [])
    missing_coverage = [
        dict(meeting)
        for meeting in meetings
        if str(meeting.get('extracted_coverage_status') or 'missing') == 'missing'
    ]
    selected_meetings = missing_coverage[:slice_size]
    videos = load_video_catalog(video_catalog_path, session=session)

    vote_items_before = _read_jsonl(vote_items_path)
    vote_records_before = _read_jsonl(vote_records_path)
    meeting_results: list[dict[str, Any]] = []
    recovered_vote_items: list[dict[str, Any]] = []
    recovered_vote_records: list[dict[str, Any]] = []
    matched_video_urls: list[str] = []
    failure_counts: dict[str, int] = {
        'no_matching_video': 0,
        'no_transcript': 0,
        'transcript_unusable': 0,
        'no_vote_content': 0,
        'other': 0,
    }

    for meeting in selected_meetings:
        meeting.setdefault('meeting_type', _meeting_type(str(meeting.get('meeting_title') or '')))
        result: dict[str, Any] = {
            'meeting_date': meeting.get('meeting_date'),
            'meeting_title': meeting.get('meeting_title'),
            'meeting_type': meeting.get('meeting_type'),
        }
        matched_video = match_meeting_to_video(meeting, videos)
        if matched_video is None:
            result['miss_class'] = 'no_matching_video'
            result['miss_detail'] = 'no official BCBE Board Videos match for meeting date/title'
            failure_counts['no_matching_video'] += 1
            meeting_results.append(result)
            continue

        result['matched_video_url'] = matched_video.get('video_url')
        result['matched_video_title'] = matched_video.get('title')
        result['matched_video_id'] = matched_video.get('video_id')
        matched_video_urls.append(str(matched_video.get('video_url')))

        transcript_rows, miss_class, miss_detail = fetch_transcript_rows(
            str(matched_video.get('video_id') or ''),
            transcript_client=transcript_client,
        )
        if miss_class is not None:
            result['miss_class'] = miss_class
            result['miss_detail'] = miss_detail
            failure_counts[miss_class] += 1
            meeting_results.append(result)
            continue

        vote_items = extract_vote_items(meeting, matched_video, transcript_rows or [])
        if not vote_items:
            result['miss_class'] = 'no_vote_content'
            result['miss_detail'] = 'transcript/captions were reachable but contained no vote-bearing language'
            result['transcript_row_count'] = len(transcript_rows or [])
            failure_counts['no_vote_content'] += 1
            meeting_results.append(result)
            continue

        vote_records = build_vote_records(vote_items)
        result['transcript_row_count'] = len(transcript_rows or [])
        result['recovered_vote_items'] = len(vote_items)
        result['recovered_vote_records'] = len(vote_records)
        recovered_vote_items.extend(vote_items)
        recovered_vote_records.extend(vote_records)
        meeting_results.append(result)

    vote_items_after = _upsert(vote_items_before, recovered_vote_items)
    vote_records_after = _upsert(vote_records_before, recovered_vote_records)
    _write_jsonl(vote_items_path, vote_items_after)
    _write_jsonl(vote_records_path, vote_records_after)
    _write_jsonl(meeting_results_path, meeting_results)

    completed = sum(1 for row in meeting_results if row.get('miss_class') in (None, 'no_vote_content'))
    failed = len(meeting_results) - completed
    summary = {
        'meetings_attempted': len(selected_meetings),
        'meetings_completed': completed,
        'meetings_failed': failed,
        'matched_video_urls': list(dict.fromkeys(matched_video_urls)),
        'vote_items_before': len(vote_items_before),
        'vote_items_after': len(vote_items_after),
        'vote_records_before': len(vote_records_before),
        'vote_records_after': len(vote_records_after),
        'failure_counts_by_class': failure_counts,
    }
    summary['verdict'] = summarize_verdict(summary)
    _write_json(summary_path, summary)
    return summary
