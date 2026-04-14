from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = ROOT / "skills" / "btc-sprint-stack" / "adapters"
ENGINE = ROOT / "skills" / "btc-sprint-stack" / "engine"
REPLAY = ROOT / "skills" / "btc-sprint-stack" / "replay"
for path in (ADAPTERS, ENGINE, REPLAY):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest

from contracts import MarketMetadata
from runner import ReplayRunner, serialize_events


@dataclass
class FixtureAdapter:
    markets: list[MarketMetadata] | None = None
    error: Exception | None = None

    def list_open_markets(self):
        if self.error:
            raise self.error
        return list(self.markets or [])

    def get_market(self, market_id: str) -> MarketMetadata:
        for market in self.markets or []:
            if market.market_id == market_id:
                return market
        raise KeyError(market_id)

    def healthcheck(self):
        return {"status": "ok"}


def _market(*, market_id: str, open_time: datetime, close_time: datetime) -> MarketMetadata:
    return MarketMetadata(
        market_id=market_id,
        condition_id=f"cond-{market_id}",
        question="Will BTC be up in 15m?",
        yes_token_id=f"yes-{market_id}",
        no_token_id=f"no-{market_id}",
        open_time=open_time,
        close_time=close_time,
        tags=("btc", "15m"),
    )


def test_replay_runner_happy_path_emits_complete_ordered_sequence():
    now = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    adapter = FixtureAdapter(
        markets=[_market(market_id="m1", open_time=now - timedelta(minutes=5), close_time=now + timedelta(minutes=10))]
    )
    rounds = [{"round_id": "r1", "ts_utc": now.isoformat(), "candidate_market_ids": ["m1"], "health": {"stale": False}}]

    runner = ReplayRunner(adapter)
    results = runner.run(rounds)
    events = serialize_events(runner.writer.events)

    assert [e["event_type"] for e in events] == [
        "round_start",
        "market_selected",
        "replay_feed_status",
        "decision_placeholder",
        "round_complete",
    ]
    assert results[0].terminal_state == "completed"
    assert results[0].selected_market_id == "m1"


def test_replay_runner_malformed_market_emits_typed_reject_event():
    adapter = FixtureAdapter(error=ValueError("Malformed timestamp: bad-ts"))
    now = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)

    runner = ReplayRunner(adapter)
    results = runner.run([{"round_id": "r1", "ts_utc": now.isoformat(), "candidate_market_ids": ["mX"], "health": {"stale": False}}])
    events = serialize_events(runner.writer.events)

    reject = next(e for e in events if e["event_type"] == "market_rejected")
    assert reject["payload"]["reject_type"] == "MalformedMarketError"
    assert results[0].terminal_state == "rejected"


def test_replay_runner_no_eligible_market_has_explicit_terminal_reject():
    now = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
    closed = _market(market_id="m-closed", open_time=now - timedelta(minutes=20), close_time=now - timedelta(minutes=5))
    adapter = FixtureAdapter(markets=[closed])

    runner = ReplayRunner(adapter)
    results = runner.run([{"round_id": "r1", "ts_utc": now.isoformat(), "candidate_market_ids": ["m-closed"], "health": {"stale": False}}])
    events = serialize_events(runner.writer.events)

    assert results[0].terminal_state == "rejected"
    assert events[-1]["event_type"] == "round_complete"
    assert events[-1]["payload"]["terminal_state"] == "rejected"


def test_replay_runner_is_deterministic_for_identical_fixtures():
    now = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    adapter = FixtureAdapter(
        markets=[_market(market_id="m1", open_time=now - timedelta(minutes=5), close_time=now + timedelta(minutes=10))]
    )
    rounds = [{"round_id": "r1", "ts_utc": now.isoformat(), "candidate_market_ids": ["m1"], "health": {"stale": False}}]

    first = ReplayRunner(adapter)
    second = ReplayRunner(adapter)

    first_results = [r.__dict__ for r in first.run(rounds)]
    second_results = [r.__dict__ for r in second.run(rounds)]
    assert first_results == second_results
    assert serialize_events(first.writer.events) == serialize_events(second.writer.events)


def test_replay_runner_no_silent_skip_every_round_has_terminal_outcome():
    now = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    open_market = _market(market_id="m-open", open_time=now - timedelta(minutes=5), close_time=now + timedelta(minutes=10))
    adapter = FixtureAdapter(markets=[open_market])
    rounds = [
        {"round_id": "r1", "ts_utc": now.isoformat(), "candidate_market_ids": ["m-open"], "health": {"stale": False}},
        {"round_id": "r2", "ts_utc": now.isoformat(), "candidate_market_ids": ["m-open"], "health": {"stale": True}},
    ]

    runner = ReplayRunner(adapter)
    runner.run(rounds)
    events = serialize_events(runner.writer.events)

    terminal = [e for e in events if e["event_type"] == "round_complete"]
    assert len(terminal) == len(rounds)
    assert {e["round_id"] for e in terminal} == {"r1", "r2"}
