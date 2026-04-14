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
from decision_handoff import FeatureSnapshot, create_decision_record
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
            results.append(self._run_round(round_input))
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

        snapshot = FeatureSnapshot(
            round_id=round_id,
            market_id=selected_market_id or "unselected_market",
            ts_utc=round_ts,
            feed_status=feed_status,
            sufficient_data=bool(round_input.get("sufficient_data", selected_market_id is not None and not stale)),
            stale=stale,
            malformed=bool(round_input.get("malformed", False)),
            fully_scored=bool(round_input.get("fully_scored", False)),
            feature_summary={
                "candidate_market_ids": candidate_ids,
                "selected_market_id": selected_market_id,
                "health": health,
            },
        )
        self.writer.append(
            ReplayEvent(
                event_type="feature_snapshot_created",
                round_id=round_id,
                ts_utc=round_ts,
                payload={
                    "market_id": snapshot.market_id,
                    "sufficient_data": snapshot.sufficient_data,
                    "stale": snapshot.stale,
                    "malformed": snapshot.malformed,
                    "fully_scored": snapshot.fully_scored,
                },
            )
        )

        decision = create_decision_record(snapshot)
        if decision.final_action == "no_trade":
            decision_event_type = "no_trade_recorded"
            if terminal_state != "rejected":
                terminal_state = "no_trade"
        else:
            decision_event_type = "decision_recorded"

        self.writer.append(
            ReplayEvent(
                event_type=decision_event_type,
                round_id=round_id,
                ts_utc=round_ts,
                payload={
                    "market_id": decision.market_id,
                    "vote_summary": decision.vote_summary,
                    "no_trade_basis": decision.no_trade_basis,
                    "edge_placeholder": decision.edge_placeholder,
                    "edge_unavailable_reason": decision.edge_unavailable_reason,
                    "gate_result": decision.gate_result,
                    "final_action": decision.final_action,
                },
            )
        )

        action = decision.final_action if terminal_state != "rejected" else "reject_no_trade"

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
