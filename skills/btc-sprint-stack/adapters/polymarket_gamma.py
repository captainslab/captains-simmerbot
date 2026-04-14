from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import urlopen

from contracts import MarketMetadata, parse_utc_timestamp


class MarketNormalizationError(ValueError):
    pass


def _default_fetch_json(url: str, timeout: int = 10) -> list[dict[str, Any]]:
    with urlopen(url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    raise MarketNormalizationError("Gamma response must be a list or {data: list}")


@dataclass
class PolymarketGammaAdapter:
    base_url: str = "https://gamma-api.polymarket.com"
    fetch_json: Callable[[str], list[dict[str, Any]]] = _default_fetch_json

    def list_open_markets(self) -> Sequence[MarketMetadata]:
        query = urlencode({"active": "true", "closed": "false", "limit": 200})
        url = f"{self.base_url.rstrip('/')}/markets?{query}"
        payload = self.fetch_json(url)

        normalized: list[MarketMetadata] = []
        for row in payload:
            if not self._looks_like_btc_15m(row):
                continue
            normalized.append(self._to_market_metadata(row))
        return normalized

    def get_market(self, market_id: str) -> MarketMetadata:
        url = f"{self.base_url.rstrip('/')}/markets/{market_id}"
        payload = self.fetch_json(url)
        if not payload:
            raise MarketNormalizationError(f"Gamma market not found: {market_id}")
        return self._to_market_metadata(payload[0])

    def healthcheck(self) -> Mapping[str, Any]:
        status = "ok"
        error: str | None = None
        try:
            self.list_open_markets()
        except Exception as exc:  # pragma: no cover - defensive network path
            status = "error"
            error = str(exc)
        return {"provider": "gamma", "status": status, "error": error}

    @staticmethod
    def _looks_like_btc_15m(row: Mapping[str, Any]) -> bool:
        text = " ".join(
            str(row.get(key, ""))
            for key in ("question", "title", "description", "slug")
        ).lower()
        return "btc" in text and ("15m" in text or "15-minute" in text or "15 minute" in text)

    @staticmethod
    def _extract_token_mapping(row: Mapping[str, Any]) -> tuple[str, str]:
        yes_token: str | None = None
        no_token: str | None = None

        tokens = row.get("tokens")
        if isinstance(tokens, list):
            for token in tokens:
                outcome = str(token.get("outcome", "")).strip().lower()
                token_id = str(token.get("token_id") or token.get("id") or "").strip()
                if outcome == "yes" and token_id:
                    yes_token = token_id
                elif outcome == "no" and token_id:
                    no_token = token_id

        clob_ids = row.get("clobTokenIds")
        if (not yes_token or not no_token) and isinstance(clob_ids, str):
            try:
                parsed = json.loads(clob_ids)
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list) and len(parsed) >= 2:
                yes_token = yes_token or str(parsed[0]).strip()
                no_token = no_token or str(parsed[1]).strip()

        if not yes_token or not no_token:
            raise MarketNormalizationError("Missing YES/NO token mapping")
        if yes_token == no_token:
            raise MarketNormalizationError("Ambiguous YES/NO token mapping")
        return yes_token, no_token

    def _to_market_metadata(self, row: Mapping[str, Any]) -> MarketMetadata:
        market_id = str(row.get("id") or row.get("market_id") or "").strip()
        condition_id = str(row.get("conditionId") or row.get("condition_id") or "").strip()
        question = str(row.get("question") or row.get("title") or "").strip()

        open_raw = row.get("startDate") or row.get("start_date") or row.get("openTime") or row.get("open_time")
        close_raw = row.get("endDate") or row.get("end_date") or row.get("closeTime") or row.get("close_time")

        if not market_id or not condition_id or not question:
            raise MarketNormalizationError("Missing required market identifiers")
        if open_raw is None or close_raw is None:
            raise MarketNormalizationError("Missing open/close timestamps")

        yes_token_id, no_token_id = self._extract_token_mapping(row)

        tags_raw = row.get("tags")
        tags: tuple[str, ...]
        if isinstance(tags_raw, list):
            tags = tuple(str(t) for t in tags_raw)
        else:
            tags = tuple()

        try:
            open_time = parse_utc_timestamp(open_raw)
            close_time = parse_utc_timestamp(close_raw)
        except ValueError as exc:
            raise MarketNormalizationError(str(exc)) from exc

        return MarketMetadata(
            market_id=market_id,
            condition_id=condition_id,
            question=question,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            open_time=open_time,
            close_time=close_time,
            tags=tags,
        )
