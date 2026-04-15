"""
btc_skill_stack.py — Dynamic signal-skill registry for btc-sprint-stack.

Each skill defines a named signal strategy (RSI, MA cross, breakout, VWAP,
or momentum variant) with tunable parameters and a blending weight.  Skills
are persisted in data/skill_stack/registry.json (the sole source of truth)
and an individual SKILL.md per entry (documentation only).

Safety rules
------------
* New skills are created **disabled** by default.  The user must explicitly
  ``enable skill <name>`` before the skill influences live trading.
* registry.json is written atomically (temp file + os.replace) to prevent
  partial-state reads by the main trading loop.
* Klines are cached per (symbol, interval) within a single blending call
  to avoid redundant Binance API calls when multiple skills share the same
  data series.

Signal blending
---------------
Rather than averaging raw ``edge`` values across heterogeneous indicators
(which would be meaningless since each indicator has its own scale), blending
uses a **net directional score**:

    net = Σ(direction_i × weight_i × edge_i) / Σ(weight_i)
    where direction_i ∈ {+1 (yes), -1 (no), 0 (hold)}

The merged ``action`` is "yes" / "no" / "hold" depending on net's sign and
magnitude relative to ``min_edge``.  The merged ``edge`` is ``|net|``, and
``confidence`` is a weighted average of constituent confidences (a rough
proxy, not a calibrated probability).

The constituent signals are stored verbatim in the blended signal's
``metrics`` dict under the key ``"constituents"`` for audit/attribution.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btc_sprint_signal import (
    SignalDecision,
    _pct_change,
    _realized_volatility,
    fetch_binance_klines,
)

REGISTRY_FILENAME = "registry.json"
SKILL_STACK_DIR = "skill_stack"
SIGNAL_TYPES = frozenset({"momentum", "rsi", "ma_cross", "breakout", "vwap"})


@dataclass
class SkillEntry:
    id: str
    name: str
    description: str
    signal_type: str
    params: dict = field(default_factory=dict)
    weight: float = 1.0
    enabled: bool = False  # disabled by default — user must explicitly enable
    created_at: str = ""
    created_via: str = "discord"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SkillEntry":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})


# ---------------------------------------------------------------------------
# Registry I/O (atomic writes)
# ---------------------------------------------------------------------------

def load_registry(data_dir: Path) -> list[SkillEntry]:
    registry_path = data_dir / SKILL_STACK_DIR / REGISTRY_FILENAME
    if not registry_path.exists():
        return []
    try:
        payload = json.loads(registry_path.read_text())
        return [SkillEntry.from_dict(e) for e in payload.get("skills", [])]
    except Exception:
        return []


def save_registry(data_dir: Path, entries: list[SkillEntry]) -> None:
    """Write registry.json atomically via temp file + os.replace."""
    stack_dir = data_dir / SKILL_STACK_DIR
    stack_dir.mkdir(parents=True, exist_ok=True)
    registry_path = stack_dir / REGISTRY_FILENAME
    payload = {
        "skills": [e.to_dict() for e in entries],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    content = json.dumps(payload, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=stack_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_path, registry_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_active_skills(data_dir: Path) -> list[SkillEntry]:
    return [e for e in load_registry(data_dir) if e.enabled and e.signal_type != "agent"]


def _scan_installed_skills(bot_root: Path) -> list[tuple[str, str]]:
    """Return (name, description) pairs from SKILL.md files in skills/ and .agents/skills/."""
    results: list[tuple[str, str]] = []
    search_dirs = [
        bot_root / "skills",
        bot_root / ".agents" / "skills",
    ]
    for base in search_dirs:
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            try:
                text = skill_md.read_text()
                name = skill_md.parent.name
                desc = ""
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("description:"):
                        desc = line[len("description:"):].strip().strip('"').strip("'")
                        break
                results.append((name, desc or "—"))
            except Exception:
                pass
    return results


# ---------------------------------------------------------------------------
# Skill creation helpers
# ---------------------------------------------------------------------------

def _sanitize_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _detect_signal_type(description: str) -> str:
    desc = description.lower()
    if re.search(r"\brsi\b", desc):
        return "rsi"
    if re.search(r"\bema\b|\bma\s*cross\b|\bma-cross\b|\bmoving average cross", desc):
        return "ma_cross"
    if re.search(r"\bbreakout\b|\bdonchian\b|\bchannel break", desc):
        return "breakout"
    if re.search(r"\bvwap\b", desc):
        return "vwap"
    return "momentum"


def _default_params(signal_type: str) -> dict:
    defaults: dict[str, dict] = {
        "momentum": {"short_window": 3, "long_window": 8, "interval": "1m", "limit": 30},
        "rsi": {"period": 14, "oversold": 30, "overbought": 70, "interval": "1m", "limit": 50},
        "ma_cross": {"short_ema": 5, "long_ema": 13, "interval": "1m", "limit": 30},
        "breakout": {"period": 20, "interval": "1m", "limit": 30},
        "vwap": {"threshold": 0.003, "interval": "1m", "limit": 30},
    }
    return dict(defaults.get(signal_type, {}))


def _extract_params(description: str, signal_type: str) -> dict:
    params = _default_params(signal_type)
    desc = description.lower()
    if signal_type == "rsi":
        m = re.search(r"rsi[\s(]+(\d+)", desc)
        if m:
            params["period"] = int(m.group(1))
        m = re.search(r"(?:oversold|below|under|<)\s*(\d+)", desc)
        if m:
            params["oversold"] = int(m.group(1))
        m = re.search(r"(?:overbought|above|over|>)\s*(\d+)", desc)
        if m:
            params["overbought"] = int(m.group(1))
    elif signal_type == "ma_cross":
        nums = [int(n) for n in re.findall(r"\b(\d+)\b", desc) if 2 <= int(n) <= 100]
        if len(nums) >= 2:
            params["short_ema"] = min(nums[:2])
            params["long_ema"] = max(nums[:2])
    elif signal_type == "breakout":
        m = re.search(r"(\d+)\s*(?:period|candle|bar)", desc)
        if m:
            params["period"] = int(m.group(1))
    elif signal_type == "vwap":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", desc)
        if m:
            params["threshold"] = float(m.group(1)) / 100
    elif signal_type == "momentum":
        nums = [int(n) for n in re.findall(r"\b(\d+)\b", desc) if 2 <= int(n) <= 100]
        if len(nums) >= 2:
            params["short_window"] = min(nums[:2])
            params["long_window"] = max(nums[:2])
    return params


def _generate_skill_md(entry: SkillEntry) -> str:
    params_str = "\n".join(f"  {k}: {v}" for k, v in entry.params.items())
    return (
        f"---\n"
        f"id: {entry.id}\n"
        f"name: {entry.name}\n"
        f"signal_type: {entry.signal_type}\n"
        f"weight: {entry.weight}\n"
        f"enabled: {str(entry.enabled).lower()}\n"
        f"created_at: {entry.created_at}\n"
        f"created_via: {entry.created_via}\n"
        f"---\n"
        f"# {entry.name}\n\n"
        f"{entry.description}\n\n"
        f"## Signal Type\n**{entry.signal_type}**\n\n"
        f"## Parameters\n{params_str}\n\n"
        f"## Notes\n"
        f"Created via Discord.  **Disabled by default** — use "
        f"`enable skill {entry.id}` to activate.  When active, its signal\n"
        f"is blended with the base momentum signal using net directional scoring.\n"
    )


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def create_skill(
    data_dir: Path,
    name: str,
    description: str,
    signal_type: str | None = None,
    params: dict | None = None,
    weight: float = 1.0,
) -> SkillEntry:
    """Scaffold and persist a new (disabled) skill.

    Raises ValueError if a skill with the same slug already exists.
    """
    skill_id = _sanitize_id(name)
    entries = load_registry(data_dir)
    if any(e.id == skill_id for e in entries):
        raise ValueError(
            f"Skill '{skill_id}' already exists. "
            "Use 'enable/disable skill' or choose a different name."
        )
    detected_type = signal_type or _detect_signal_type(description)
    if detected_type not in SIGNAL_TYPES:
        detected_type = "momentum"
    computed_params = _extract_params(description, detected_type)
    if params:
        computed_params.update(params)
    entry = SkillEntry(
        id=skill_id,
        name=name,
        description=description,
        signal_type=detected_type,
        params=computed_params,
        weight=weight,
        enabled=False,  # disabled until user explicitly enables
        created_at=datetime.now(timezone.utc).isoformat(),
        created_via="discord",
    )
    entries.append(entry)
    save_registry(data_dir, entries)  # atomic — write registry last
    # Write SKILL.md after registry is committed (decorative; not load-critical)
    skill_dir = data_dir / SKILL_STACK_DIR / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_generate_skill_md(entry))
    return entry


def remove_skill(data_dir: Path, name: str) -> SkillEntry | None:
    skill_id = _sanitize_id(name)
    entries = load_registry(data_dir)
    entry = next((e for e in entries if e.id == skill_id), None)
    if entry is None:
        return None
    save_registry(data_dir, [e for e in entries if e.id != skill_id])
    return entry


def set_skill_enabled(data_dir: Path, name: str, enabled: bool) -> SkillEntry | None:
    skill_id = _sanitize_id(name)
    entries = load_registry(data_dir)
    entry = next((e for e in entries if e.id == skill_id), None)
    if entry is None:
        return None
    entry.enabled = enabled
    save_registry(data_dir, entries)
    return entry


# ---------------------------------------------------------------------------
# Signal computation per skill type
# ---------------------------------------------------------------------------

def _compute_rsi(closes: list[float], period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    changes = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _compute_ema(closes: list[float], period: int) -> float:
    if not closes:
        return 0.0
    k = 2.0 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1.0 - k)
    return ema


def _compute_vwap(klines: list[dict], limit: int) -> float:
    rows = klines[-limit:]
    cum_tp_vol = sum((r["high"] + r["low"] + r["close"]) / 3.0 * r["volume"] for r in rows)
    cum_vol = sum(r["volume"] for r in rows)
    return cum_tp_vol / cum_vol if cum_vol > 0 else 0.0


def _compute_skill_signal(
    entry: SkillEntry,
    symbol: str,
    min_edge: float,
    klines_cache: dict[str, list[dict]],
) -> SignalDecision:
    """Compute one skill's signal.

    *klines_cache* maps ``interval`` → klines list so multiple skills sharing
    the same interval share one Binance fetch per blending call.
    """
    p = entry.params
    interval = p.get("interval", "1m")
    limit = max(int(p.get("limit", 30)), 60)
    src = f"skill:{entry.id}"

    if interval not in klines_cache:
        try:
            klines_cache[interval] = fetch_binance_klines(
                symbol=symbol, interval=interval, limit=limit
            )
        except Exception as exc:
            return SignalDecision(
                action="hold", edge=0.0, confidence=0.5, signal_source=src,
                reasoning=f"Skill {entry.id}: Binance fetch failed: {exc}",
                metrics={},
            )
    klines = klines_cache[interval]
    closes = [r["close"] for r in klines]

    if entry.signal_type == "rsi":
        period = int(p.get("period", 14))
        oversold = float(p.get("oversold", 30))
        overbought = float(p.get("overbought", 70))
        rsi = _compute_rsi(closes, period)
        if rsi < oversold:
            action = "yes"
            edge = min(0.18, (oversold - rsi) / oversold * 0.2)
            confidence = min(0.90, 0.60 + (oversold - rsi) / oversold * 0.30)
        elif rsi > overbought:
            action = "no"
            edge = min(0.18, (rsi - overbought) / (100.0 - overbought) * 0.2)
            confidence = min(0.90, 0.60 + (rsi - overbought) / (100.0 - overbought) * 0.30)
        else:
            action, edge, confidence = "hold", 0.0, 0.50
        if edge < min_edge:
            action, edge = "hold", 0.0
        return SignalDecision(
            action=action, edge=edge, confidence=confidence, signal_source=src,
            reasoning=f"RSI({period})={rsi:.1f} [oversold={oversold}, overbought={overbought}].",
            metrics={"rsi": rsi},
        )

    if entry.signal_type == "ma_cross":
        short = int(p.get("short_ema", 5))
        long_p = int(p.get("long_ema", 13))
        ema_s = _compute_ema(closes, short)
        ema_l = _compute_ema(closes, long_p)
        diff = (ema_s - ema_l) / ema_l if ema_l != 0 else 0.0
        edge = min(0.18, abs(diff) * 20.0)
        confidence = min(0.90, 0.55 + abs(diff) * 15.0)
        if edge >= min_edge:
            action = "yes" if diff > 0 else "no"
        else:
            action, edge = "hold", 0.0
        return SignalDecision(
            action=action, edge=edge, confidence=confidence, signal_source=src,
            reasoning=f"EMA({short})={ema_s:.2f} vs EMA({long_p})={ema_l:.2f}, diff={diff:+.4f}.",
            metrics={"ema_short": ema_s, "ema_long": ema_l, "diff": diff},
        )

    if entry.signal_type == "breakout":
        period = int(p.get("period", 20))
        recent = klines[-period:] if len(klines) >= period else klines
        high = max(r["high"] for r in recent)
        low = min(r["low"] for r in recent)
        current = closes[-1]
        ch_width = high - low
        if ch_width == 0:
            return SignalDecision(action="hold", edge=0.0, confidence=0.5, signal_source=src,
                                  reasoning="Zero Donchian channel width.", metrics={})
        pos = (current - low) / ch_width
        if pos > 0.85:
            action = "yes"
            edge = min(0.15, (pos - 0.85) * 2.0)
        elif pos < 0.15:
            action = "no"
            edge = min(0.15, (0.15 - pos) * 2.0)
        else:
            action, edge = "hold", 0.0
        confidence = min(0.85, 0.55 + edge * 1.5)
        if edge < min_edge:
            action, edge = "hold", 0.0
        return SignalDecision(
            action=action, edge=edge, confidence=confidence, signal_source=src,
            reasoning=f"Donchian({period}): pos={pos:.2f}, H={high:.2f}, L={low:.2f}.",
            metrics={"channel_pos": pos, "high": high, "low": low},
        )

    if entry.signal_type == "vwap":
        threshold = float(p.get("threshold", 0.003))
        vwap = _compute_vwap(klines, int(p.get("limit", 30)))
        current = closes[-1]
        diff = (current - vwap) / vwap if vwap != 0 else 0.0
        edge = min(0.15, abs(diff) * 8.0)
        confidence = min(0.88, 0.55 + abs(diff) * 6.0)
        if abs(diff) >= threshold and edge >= min_edge:
            action = "yes" if diff < 0 else "no"  # mean-reversion: buy below VWAP
        else:
            action, edge = "hold", 0.0
        return SignalDecision(
            action=action, edge=edge, confidence=confidence, signal_source=src,
            reasoning=f"VWAP={vwap:.2f}, price={current:.2f}, diff={diff:+.4f}.",
            metrics={"vwap": vwap, "current": current, "diff": diff},
        )

    # momentum (default)
    short = int(p.get("short_window", 3))
    long_p = int(p.get("long_window", 8))
    if len(closes) < long_p:
        return SignalDecision(action="hold", edge=0.0, confidence=0.5, signal_source=src,
                              reasoning="Insufficient data for momentum.", metrics={})
    short_move = _pct_change(closes[-short], closes[-1])
    long_move = _pct_change(closes[-long_p], closes[-1])
    vol = _realized_volatility(closes[-long_p:])
    combined = (short_move * 0.6) + (long_move * 0.4)
    edge = max(0.0, min(0.2, abs(combined) * 22.0))
    confidence = max(0.5, min(0.95, 0.58 + abs(combined) * 18.0 - vol * 8.0))
    if edge >= min_edge:
        action = "yes" if combined > 0 else "no"
    else:
        action, edge = "hold", 0.0
    return SignalDecision(
        action=action, edge=edge, confidence=confidence, signal_source=src,
        reasoning=f"Momentum: short={short_move:+.4f}, long={long_move:+.4f}, vol={vol:.4f}.",
        metrics={"short_move": short_move, "long_move": long_move, "volatility": vol},
    )


# ---------------------------------------------------------------------------
# Signal blending — net directional score
# ---------------------------------------------------------------------------

def blend_signals(
    signals: list[tuple[SignalDecision, float]],
    min_edge: float = 0.0,
) -> SignalDecision:
    """Blend signals using a **net directional score**.

    net = Σ(direction_i × weight_i × edge_i) / Σ(weight_i)
    direction: yes→+1, no→−1, hold→0

    The merged action is "yes"/"no"/"hold" based on sign(net) and |net|≥min_edge.
    Merged edge = |net|.  Merged confidence = weighted mean of confidences.
    Constituents are stored in metrics["constituents"] for audit.
    """
    if not signals:
        raise ValueError("blend_signals requires at least one signal")
    if len(signals) == 1:
        return signals[0][0]

    _DIR = {"yes": 1.0, "no": -1.0, "hold": 0.0}
    total_w = sum(w for _, w in signals) or float(len(signals))
    net_score = 0.0
    w_conf = 0.0
    constituents: list[dict] = []
    for sig, w in signals:
        direction = _DIR.get(sig.action, 0.0)
        net_score += direction * w * sig.edge
        w_conf += sig.confidence * w
        constituents.append({
            "source": sig.signal_source,
            "action": sig.action,
            "edge": sig.edge,
            "confidence": sig.confidence,
            "weight": w,
        })
    net_score /= total_w
    w_conf /= total_w

    merged_edge = round(abs(net_score), 4)
    merged_conf = round(w_conf, 4)
    if merged_edge >= min_edge and not math.isclose(net_score, 0.0):
        merged_action = "yes" if net_score > 0 else "no"
    else:
        merged_action = "hold"
        merged_edge = 0.0

    sources = "+".join(sig.signal_source for sig, _ in signals)
    all_reasoning = "; ".join(sig.reasoning for sig, _ in signals)
    return SignalDecision(
        action=merged_action,
        edge=merged_edge,
        confidence=merged_conf,
        signal_source=f"blended:{sources}",
        reasoning=f"Net directional score={net_score:+.4f} over {len(signals)} signals. {all_reasoning}",
        metrics={"net_score": net_score, "constituents": constituents},
    )


def build_blended_signal(
    base_signal: SignalDecision,
    data_dir: Path,
    window: str,
    symbol: str,
    min_edge: float,
) -> SignalDecision:
    """Compute active-skill signals and blend with *base_signal*.

    Returns *base_signal* unchanged when no enabled skills are registered.
    Klines are cached per interval within this call to minimise Binance API calls.
    """
    active = get_active_skills(data_dir)
    if not active:
        return base_signal
    klines_cache: dict[str, list[dict]] = {}
    pairs: list[tuple[SignalDecision, float]] = [(base_signal, 1.0)]
    for skill in active:
        sig = _compute_skill_signal(skill, symbol=symbol, min_edge=min_edge, klines_cache=klines_cache)
        pairs.append((sig, skill.weight))
    return blend_signals(pairs, min_edge=min_edge)


# ---------------------------------------------------------------------------
# Discord skill-command parsing (pure — no side effects)
# ---------------------------------------------------------------------------

_ADD_RE = re.compile(
    r"(?:add|create|new)\s+skill\s+([a-zA-Z0-9_\- ]+?)\s*:\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)
_REMOVE_RE = re.compile(r"(?:remove|delete|drop)\s+skill\s+([a-zA-Z0-9_\- ]+)", re.IGNORECASE)
_ENABLE_RE = re.compile(r"enable\s+skill\s+([a-zA-Z0-9_\- ]+)", re.IGNORECASE)
_DISABLE_RE = re.compile(r"disable\s+skill\s+([a-zA-Z0-9_\- ]+)", re.IGNORECASE)
_LIST_RE = re.compile(r"(?:list|show)\s+skills?\b", re.IGNORECASE)
_SHOW_SKILL_RE = re.compile(r"show\s+skill\s+([a-zA-Z0-9_\- ]+)", re.IGNORECASE)


def parse_skill_command(text: str) -> dict | None:
    """Return a skill-command dict or None if *text* is not a skill command.

    Command dict shapes::

        {"op": "add",     "name": ..., "description": ...}
        {"op": "remove",  "name": ...}
        {"op": "enable",  "name": ...}
        {"op": "disable", "name": ...}
        {"op": "list"}
        {"op": "show",    "name": ...}
    """
    m = _ADD_RE.search(text)
    if m:
        return {"op": "add", "name": m.group(1).strip(), "description": m.group(2).strip()}
    m = _REMOVE_RE.search(text)
    if m:
        return {"op": "remove", "name": m.group(1).strip()}
    m = _ENABLE_RE.search(text)
    if m:
        return {"op": "enable", "name": m.group(1).strip()}
    m = _DISABLE_RE.search(text)
    if m:
        return {"op": "disable", "name": m.group(1).strip()}
    # Check show skill <name> before list (both start with "show skill")
    m = _SHOW_SKILL_RE.search(text)
    if m:
        return {"op": "show", "name": m.group(1).strip()}
    if _LIST_RE.search(text):
        return {"op": "list"}
    return None


def execute_skill_command(cmd: dict, data_dir: Path) -> str:
    """Run a skill command against *data_dir*; return human-readable Discord reply."""
    op = cmd.get("op")

    if op == "add":
        try:
            entry = create_skill(data_dir, name=cmd["name"], description=cmd["description"])
        except ValueError as exc:
            return f"⚠️ {exc}"
        params_preview = ", ".join(f"{k}={v}" for k, v in entry.params.items())
        return (
            f"📋 Skill **{entry.name}** (`{entry.id}`) created.\n"
            f"Signal: `{entry.signal_type}` | Params: {params_preview}\n"
            f"⚠️ **Disabled by default.** To activate: `enable skill {entry.id}`"
        )

    if op == "remove":
        entry = remove_skill(data_dir, cmd["name"])
        if entry is None:
            return f"⚠️ No skill found: `{cmd['name']}`."
        return f"🗑️ Skill **{entry.name}** (`{entry.id}`) removed."

    if op == "enable":
        entry = set_skill_enabled(data_dir, cmd["name"], enabled=True)
        if entry is None:
            return f"⚠️ No skill found: `{cmd['name']}`."
        params_preview = ", ".join(f"{k}={v}" for k, v in entry.params.items())
        return (
            f"✅ Skill **{entry.name}** (`{entry.id}`) **enabled** and active.\n"
            f"Signal: `{entry.signal_type}` | Params: {params_preview}\n"
            f"Its signal will be blended with base momentum on the next cycle."
        )

    if op == "disable":
        entry = set_skill_enabled(data_dir, cmd["name"], enabled=False)
        if entry is None:
            return f"⚠️ No skill found: `{cmd['name']}`."
        return f"⏸️ Skill **{entry.name}** (`{entry.id}`) disabled."

    if op == "list":
        lines = []

        # ── Installed Clawhub skills (skills/ and .agents/skills/ dirs) ──────
        bot_root = data_dir.parent.parent.parent  # data/ -> btc-sprint-stack/ -> skills/ -> root
        installed = _scan_installed_skills(bot_root)
        if installed:
            lines.append("**Installed skills:**")
            for name, desc in installed:
                lines.append(f"  • `{name}` — {desc[:90]}")

        # ── Dynamic signal registry ───────────────────────────────────────────
        entries = load_registry(data_dir)
        signal_entries = [e for e in entries if e.signal_type != "agent"]
        if signal_entries:
            lines.append("\n**Signal skills (blended into trades):**")
            for e in signal_entries:
                status = "✅ active" if e.enabled else "⏸️ disabled"
                lines.append(
                    f"  • `{e.id}` [{status}] — `{e.signal_type}` "
                    f"weight={e.weight}: {e.description[:80]}"
                )

        if not lines:
            return (
                "No custom skills in the stack yet.\n"
                "Use: `add skill <name>: <description>`"
            )
        return "\n".join(lines)

    if op == "show":
        skill_id = _sanitize_id(cmd["name"])
        skill_md_path = data_dir / SKILL_STACK_DIR / skill_id / "SKILL.md"
        if skill_md_path.exists():
            return f"```\n{skill_md_path.read_text()[:1800]}\n```"
        entries = load_registry(data_dir)
        entry = next((e for e in entries if e.id == skill_id), None)
        if entry is None:
            return f"⚠️ No skill found: `{cmd['name']}`."
        return f"**{entry.name}** (`{entry.id}`) — `{entry.signal_type}`\n{entry.description}"

    return f"⚠️ Unknown skill operation: {op}"
