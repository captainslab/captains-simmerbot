from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "adapters"
ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
for _path in (ADAPTERS_DIR, ENGINE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from contracts import MarketMetadataAdapter, parse_utc_timestamp
from market_selector import MarketSelectionError, select_current_market


@dataclass(frozen=True)
class ReplayEvent:
    event_type: str
    round_id: str
    ts_utc: datetime
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoundResult:
    round_id: str
    terminal_state: str
    action: str
    selected_market_id: str | None
    reject_reason: str | None
    completed_at_utc: datetime


class ReplayEventWriter:
    """Single-writer append-only event writer for replay rounds."""

    def __init__(self) -> None:
        self._events: list[ReplayEvent] = []

    def append(self, event: ReplayEvent) -> None:
        self._events.append(event)

    @property
    def events(self) -> list[ReplayEvent]:
        return list(self._events)


class ReplayRunner:
    def __init__(self, adapter: MarketMetadataAdapter, writer: ReplayEventWriter | None = None) -> None:
        self.adapter = adapter
        self.writer = writer or ReplayEventWriter()

    def run(self, rounds: Sequence[dict[str, Any]]) -> list[RoundResult]:
        results: list[RoundResult] = []
        for round_input in rounds:
            result = self._run_round(round_input)
            results.append(result)
        return results

    def _run_round(self, round_input: dict[str, Any]) -> RoundResult:
        round_id = str(round_input["round_id"])
        round_ts = parse_utc_timestamp(round_input["ts_utc"])
        candidate_ids = [str(v) for v in round_input.get("candidate_market_ids", [])]
        health = dict(round_input.get("health", {}))

        self.writer.append(
            ReplayEvent(
                event_type="round_start",
                round_id=round_id,
                ts_utc=round_ts,
                payload={"candidate_market_ids": candidate_ids},
            )
        )

        selected_market_id: str | None = None
        reject_reason: str | None = None
        action = "no_trade"
        terminal_state = "completed"

        try:
            market = select_current_market(self.adapter, now=round_ts)
            selected_market_id = market.market_id
            self.writer.append(
                ReplayEvent(
                    event_type="market_selected",
                    round_id=round_id,
                    ts_utc=round_ts,
                    payload={
                        "selected_market_id": market.market_id,
                        "candidate_market_ids": candidate_ids,
                    },
                )
            )
        except MarketSelectionError as exc:
            reject_reason = f"{exc.__class__.__name__}: {exc}"
            terminal_state = "rejected"
            self.writer.append(
                ReplayEvent(
                    event_type="market_rejected",
                    round_id=round_id,
                    ts_utc=round_ts,
                    payload={
                        "reject_type": exc.__class__.__name__,
                        "reject_reason": str(exc),
                        "candidate_market_ids": candidate_ids,
                    },
                )
            )

        stale = bool(health.get("stale", False))
        feed_status = "stale" if stale else "ok"
        self.writer.append(
            ReplayEvent(
                event_type="replay_feed_status",
                round_id=round_id,
                ts_utc=round_ts,
                payload={"health": health, "feed_status": feed_status},
            )
        )

        if terminal_state == "rejected":
            action = "reject_no_trade"
            self.writer.append(
                ReplayEvent(
                    event_type="no_trade_placeholder",
                    round_id=round_id,
                    ts_utc=round_ts,
                    payload={"reason": reject_reason or "unknown_reject"},
                )
            )
        elif stale:
            action = "stale_no_trade"
            terminal_state = "no_trade"
            self.writer.append(
                ReplayEvent(
                    event_type="no_trade_placeholder",
                    round_id=round_id,
                    ts_utc=round_ts,
                    payload={"reason": "stale_feed"},
                )
            )
        else:
            action = "decision_pending"
            self.writer.append(
                ReplayEvent(
                    event_type="decision_placeholder",
                    round_id=round_id,
                    ts_utc=round_ts,
                    payload={"integration_status": "decision_engine_pending"},
                )
            )

        completed = ReplayEvent(
            event_type="round_complete",
            round_id=round_id,
            ts_utc=round_ts,
            payload={
                "terminal_state": terminal_state,
                "action": action,
                "selected_market_id": selected_market_id,
                "reject_reason": reject_reason,
                "completed_at_utc": round_ts.isoformat(),
            },
        )
        self.writer.append(completed)

        return RoundResult(
            round_id=round_id,
            terminal_state=terminal_state,
            action=action,
            selected_market_id=selected_market_id,
            reject_reason=reject_reason,
            completed_at_utc=round_ts,
        )


def serialize_events(events: Iterable[ReplayEvent]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        row = asdict(event)
        row["ts_utc"] = event.ts_utc.isoformat()
        rows.append(row)
    return rows
