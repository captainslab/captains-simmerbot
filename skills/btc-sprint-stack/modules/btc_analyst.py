"""
btc_analyst.py — LLM-powered analyst for btc-sprint-stack.

Three capabilities, all invokable via Discord:

1. **Session review** — reads the trade journal, summarizes performance, and asks
   the LLM what is working and what isn't.
   Commands: "review session", "review trades", "how did we do", "what worked"

2. **Brainstorm / gameplan** — loads current config + active skills + journal summary
   and lets the LLM think through strategy ideas with the user.
   Commands: "gameplan", "brainstorm [topic]", "let's think about [topic]"

3. **Community skill critique** — user pastes a signal description or SKILL.md blob;
   the LLM evaluates fit with the current stack and either implements it (as a
   disabled skill in btc_skill_stack) or recommends adjustments.
   Commands: "critique: [content]", "review this skill: [content]"

All LLM calls are fire-and-forget from the Discord thread; they do NOT affect live
trading directly (only the user explicitly enabling a skill does that).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btc_review_metrics import build_review_metrics, format_review_metrics

_PROVIDER_CACHE: Any = None


def _get_provider() -> Any:
    """Lazily build the LLM provider from environment variables."""
    global _PROVIDER_CACHE
    if _PROVIDER_CACHE is None:
        from btc_llm_decider import build_provider_from_env
        _PROVIDER_CACHE = build_provider_from_env()
    return _PROVIDER_CACHE


# ---------------------------------------------------------------------------
# Journal helpers
# ---------------------------------------------------------------------------

def _summarize_journal(journal_rows: list[dict], n: int = 30) -> dict:
    """Return a concise structured summary of the last *n* journal rows."""
    rows = journal_rows[-n:]
    if not rows:
        return {"trades": 0, "summary": "No trades in journal."}

    trades = [r for r in rows if r.get("decision") == "candidate"]
    skipped = [r for r in rows if r.get("execution_status") in ("skipped", "rejected", "blocked")]
    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]

    # Build compact trade list for LLM context
    trade_lines = []
    for t in trades[-15:]:
        ts = t.get("timestamp", t.get("cycle_timestamp", ""))[:16]
        side = t.get("signal_action", t.get("action", "?"))
        outcome = t.get("outcome", "pending")
        edge = t.get("signal_edge", t.get("edge", "?"))
        conf = t.get("signal_confidence", t.get("confidence", "?"))
        pnl = t.get("pnl_usd", "?")
        reason = t.get("reject_reason") or t.get("execution_status", "")
        trade_lines.append(
            f"  {ts} side={side} edge={edge} conf={conf} outcome={outcome} pnl={pnl} {reason}"
        )

    # Signal sources in use
    sources = list({t.get("signal_source", "?") for t in trades})

    # Win-rate and avg pnl
    numeric_pnls = [float(t["pnl_usd"]) for t in trades if t.get("pnl_usd") not in (None, "?", "")]
    avg_pnl = sum(numeric_pnls) / len(numeric_pnls) if numeric_pnls else None
    total_pnl = sum(numeric_pnls) if numeric_pnls else None

    return {
        "trades_total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "skipped_rejected": len(skipped),
        "win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "avg_pnl_usd": round(avg_pnl, 4) if avg_pnl is not None else None,
        "total_pnl_usd": round(total_pnl, 4) if total_pnl is not None else None,
        "signal_sources": sources,
        "recent_trades": "\n".join(trade_lines) if trade_lines else "none",
    }


def analyze_session(
    data_dir: Path,
    config: dict | None = None,
    n: int = 30,
) -> str:
    """Read journal and return a compact deterministic session review."""
    from btc_trade_journal import read_journal

    journal_path = data_dir / "journal.jsonl"
    rows = read_journal(journal_path)
    effective_config = dict(config or {})
    if not effective_config:
        defaults_path = data_dir.parent / 'config' / 'defaults.json'
        if defaults_path.exists():
            try:
                effective_config = json.loads(defaults_path.read_text())
            except Exception:
                effective_config = {}
    metrics = build_review_metrics(rows[-n:], effective_config)
    return format_review_metrics(metrics)


# ---------------------------------------------------------------------------
# 2. Brainstorm / gameplan
# ---------------------------------------------------------------------------

_BRAINSTORM_SYSTEM = """\
You are a trading strategy collaborator for a live BTC prediction-market sprint bot.
The bot trades short-horizon (5m/15m) Polymarket BTC markets with a momentum signal and
a small bankroll (~$60).  Your role: think through ideas with the user, suggest signal
variants, parameter tweaks, or risk adjustments.  Be concrete.  No generic disclaimers.
Keep your response to ~200 words max.
"""

_BRAINSTORM_USER_TMPL = """\
## Bot config
{config_summary}

## Active custom skills
{active_skills}

## Recent performance (last 20 trades)
win_rate={win_rate}  avg_pnl={avg_pnl_usd}  total_pnl={total_pnl_usd}
signal_sources={signal_sources}

## User topic / question
{topic}

Think this through and give your best strategic take.
"""


def brainstorm(
    topic: str,
    data_dir: Path,
    config: dict | None = None,
) -> str:
    """Return an LLM-generated brainstorm reply scoped to the bot's context."""
    from btc_trade_journal import read_journal
    from btc_skill_stack import get_active_skills

    journal_path = data_dir / "journal.jsonl"
    rows = read_journal(journal_path)
    summary = _summarize_journal(rows, n=20)
    config_summary = _format_config(config)
    active_skills = _format_active_skills(get_active_skills(data_dir))

    user_prompt = _BRAINSTORM_USER_TMPL.format(
        config_summary=config_summary,
        active_skills=active_skills,
        topic=topic,
        **{k: (v if v is not None else "n/a") for k, v in summary.items() if k != "recent_trades"},
    )
    try:
        provider = _get_provider()
        reply = provider.complete(system_prompt=_BRAINSTORM_SYSTEM, user_prompt=user_prompt)
        return f"🧠 **Brainstorm**\n{reply.strip()}"
    except Exception as exc:
        return f"⚠️ Brainstorm failed: {exc}"


# ---------------------------------------------------------------------------
# 3. Community skill critique
# ---------------------------------------------------------------------------

_CRITIQUE_SYSTEM = """\
You are a trading strategy evaluator for a live BTC prediction-market sprint bot.
The bot trades 5m/15m Polymarket BTC sprint markets.  Evaluate a community-sourced
trading skill/strategy the user wants to add.

Respond with a JSON object only (no prose before or after):
{
  "verdict": "implement" | "implement_adjusted" | "reject",
  "reasoning": "<2-3 sentences>",
  "signal_type": "rsi" | "ma_cross" | "breakout" | "vwap" | "momentum",
  "adjusted_params": { ... },  // only if verdict != "reject"
  "adjusted_description": "..."  // one sentence, only if implement_adjusted
}

If the skill conflicts with the current stack, is redundant, or is high risk for a
small live-money bot, say so in reasoning and set verdict to "reject".
"""

_CRITIQUE_USER_TMPL = """\
## Current bot config
{config_summary}

## Active custom skills already in stack
{active_skills}

## Community skill to evaluate
{skill_content}

Evaluate this skill. Should we implement it, implement with adjustments, or reject it?
"""


def _parse_llm_json(text: str) -> dict | None:
    """Extract and parse JSON from LLM output (handles markdown fences)."""
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


def critique_and_implement(
    skill_content: str,
    data_dir: Path,
    config: dict | None = None,
) -> str:
    """LLM critiques the community skill and optionally implements it."""
    from btc_skill_stack import create_skill, get_active_skills, parse_skill_command, SIGNAL_TYPES

    config_summary = _format_config(config)
    active_skills = _format_active_skills(get_active_skills(data_dir))

    user_prompt = _CRITIQUE_USER_TMPL.format(
        config_summary=config_summary,
        active_skills=active_skills,
        skill_content=skill_content[:2000],
    )
    try:
        provider = _get_provider()
        raw = provider.complete(system_prompt=_CRITIQUE_SYSTEM, user_prompt=user_prompt)
    except Exception as exc:
        return f"⚠️ Critique failed: {exc}"

    result = _parse_llm_json(raw)
    if result is None:
        return f"⚠️ Could not parse LLM critique response.\n```\n{raw[:600]}\n```"

    verdict = result.get("verdict", "reject")
    reasoning = result.get("reasoning", "")
    signal_type = result.get("signal_type", "momentum")
    if signal_type not in SIGNAL_TYPES:
        signal_type = "momentum"

    if verdict == "reject":
        return (
            f"🚫 **Skill rejected**\n{reasoning}\n\n"
            f"The skill was not added to the stack."
        )

    # Derive a name from the content (first line or first few words)
    name_raw = skill_content.strip().splitlines()[0][:60]
    name_raw = re.sub(r"[^a-zA-Z0-9\s\-_]", "", name_raw).strip() or "community-skill"
    description = result.get("adjusted_description") or skill_content.strip()[:200]
    params = result.get("adjusted_params") or {}

    try:
        entry = create_skill(
            data_dir,
            name=name_raw,
            description=description,
            signal_type=signal_type,
            params=params or None,
        )
    except ValueError as exc:
        return f"⚠️ Could not create skill: {exc}"

    if verdict == "implement_adjusted":
        action_label = f"implemented **with adjustments** as `{entry.id}`"
    else:
        action_label = f"implemented as-is as `{entry.id}`"

    params_preview = ", ".join(f"{k}={v}" for k, v in entry.params.items())
    return (
        f"🔍 **Skill Critique**\n"
        f"Verdict: **{verdict}**\n"
        f"{reasoning}\n\n"
        f"✅ Skill {action_label}.\n"
        f"Signal: `{entry.signal_type}` | Params: {params_preview}\n"
        f"⚠️ **Disabled** until you run: `enable skill {entry.id}`"
    )


# ---------------------------------------------------------------------------
# Analyst command parsing (pure — no side effects)
# ---------------------------------------------------------------------------

_REVIEW_RE = re.compile(
    r"(?:review\s+(?:session|trades?|performance)|how\s+did\s+we\s+do|what\s+worked|session\s+review)",
    re.IGNORECASE,
)
_BRAINSTORM_RE = re.compile(
    r"(?:gameplan|brainstorm|let[''']?s?\s+think(?:\s+about)?)\s*:?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_CRITIQUE_RE = re.compile(
    r"(?:critique|review\s+this\s+skill|evaluate\s+(?:this\s+)?skill|what\s+do\s+you\s+think\s+about\s+this\s+skill)\s*:?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)


def parse_analyst_command(text: str) -> dict | None:
    """Return an analyst-command dict or None.

    Shapes::
        {"op": "review"}
        {"op": "brainstorm", "topic": ...}
        {"op": "critique",   "content": ...}
    """
    if _REVIEW_RE.search(text):
        return {"op": "review"}
    m = _BRAINSTORM_RE.search(text)
    if m:
        topic = m.group(1).strip() or "general strategy review"
        return {"op": "brainstorm", "topic": topic}
    m = _CRITIQUE_RE.search(text)
    if m:
        content = m.group(1).strip()
        if content:
            return {"op": "critique", "content": content}
    return None


def execute_analyst_command(cmd: dict, data_dir: Path, config: dict | None = None) -> str:
    """Run an analyst command and return a Discord-ready reply."""
    op = cmd.get("op")
    if op == "review":
        return analyze_session(data_dir, config=config)
    if op == "brainstorm":
        return brainstorm(cmd.get("topic", "general strategy"), data_dir, config=config)
    if op == "critique":
        return critique_and_implement(cmd.get("content", ""), data_dir, config=config)
    return f"⚠️ Unknown analyst operation: {op}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_config(config: dict | None) -> str:
    if not config:
        return "(config not available)"
    keys = [
        "min_edge", "min_confidence", "max_trade_usd", "max_open_positions",
        "max_daily_loss_usd", "cycle_interval_minutes", "execution_profile",
        "strategy_label",
    ]
    parts = [f"{k}={config[k]}" for k in keys if k in config]
    return "  " + "\n  ".join(parts)


def _format_active_skills(skills: list) -> str:
    if not skills:
        return "none (base momentum only)"
    return "\n".join(
        f"  • {s.id} [{s.signal_type}] weight={s.weight}: {s.description[:80]}"
        for s in skills
    )
