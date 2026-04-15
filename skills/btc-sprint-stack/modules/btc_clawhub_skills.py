"""
btc_clawhub_skills.py — ClawHub skill management + discovery for Discord.

Discord commands (after stripping the simmer: prefix):
  list skills                    — installed skills + signal registry
  discover [query]               — search ClawHub registry
  install skill <slug>           — install from ClawHub
  remove skill <slug>            — uninstall from ClawHub
  inspect skill <slug>           — details + description
  skill performance              — per-skill P&L from Simmer briefing
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
_DISCOVER_RE = re.compile(
    r"(?:discover|search\s+skills?|browse\s+skills?)\s*(.*)", re.IGNORECASE
)
_INSTALL_RE  = re.compile(r"install\s+skill\s+([a-zA-Z0-9_\-]+)", re.IGNORECASE)
_REMOVE_RE   = re.compile(
    r"(?:remove|uninstall|delete)\s+skill\s+([a-zA-Z0-9_\-]+)", re.IGNORECASE
)
_INSPECT_RE  = re.compile(r"inspect\s+skill\s+([a-zA-Z0-9_\-]+)", re.IGNORECASE)
_PERF_RE     = re.compile(
    r"skill\s+(?:performance|stats?|p&l|pnl)", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bot_root(data_dir: Path) -> Path:
    """data/skill_stack/../../../  →  bot root (contains skills/ dir)."""
    return data_dir.parent.parent.parent


def _run_clawhub(args: list[str], workdir: Path, timeout: int = 60) -> tuple[int, str]:
    """Run clawhub CLI and return (returncode, combined output)."""
    cmd = ["npx", "clawhub@latest", "--workdir", str(workdir), "--no-input"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        # Strip ANSI escape codes
        output = re.sub(r"\x1b\[[0-9;]*m", "", output)
        # Strip spinner artifacts
        output = re.sub(r"[⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*", "", output)
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 1, "⏱️ Timed out waiting for clawhub CLI."
    except Exception as exc:
        return 1, f"⚠️ Failed to run clawhub: {exc}"


def _simmer_briefing(api_key: str) -> dict[str, Any]:
    try:
        r = requests.get(
            "https://api.simmer.markets/api/sdk/briefing",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Command parser
# ---------------------------------------------------------------------------

def parse_clawhub_command(text: str) -> dict | None:
    """Return a command dict or None if text is not a ClawHub command."""
    m = _INSTALL_RE.search(text)
    if m:
        return {"op": "install", "slug": m.group(1).strip()}

    m = _REMOVE_RE.search(text)
    if m:
        return {"op": "remove", "slug": m.group(1).strip()}

    m = _INSPECT_RE.search(text)
    if m:
        return {"op": "inspect", "slug": m.group(1).strip()}

    if _PERF_RE.search(text):
        return {"op": "performance"}

    m = _DISCOVER_RE.search(text)
    if m:
        return {"op": "discover", "query": m.group(1).strip()}

    return None


# ---------------------------------------------------------------------------
# Command executor
# ---------------------------------------------------------------------------

def execute_clawhub_command(cmd: dict, *, data_dir: Path) -> str:
    op = cmd["op"]
    bot_root = _bot_root(data_dir)

    # ── install ──────────────────────────────────────────────────────────────
    if op == "install":
        slug = cmd["slug"]
        skills_dir = bot_root / "skills"
        if (skills_dir / slug).exists():
            return f"⚠️ `{slug}` is already installed. Use `inspect skill {slug}` to view it."
        rc, out = _run_clawhub(["install", slug], workdir=bot_root, timeout=120)
        if rc == 0:
            return f"✅ Installed **{slug}**.\n```\n{out[:600]}\n```"
        return f"❌ Install failed for `{slug}`:\n```\n{out[:800]}\n```"

    # ── remove ───────────────────────────────────────────────────────────────
    if op == "remove":
        slug = cmd["slug"]
        skills_dir = bot_root / "skills"
        if not (skills_dir / slug).exists():
            return f"⚠️ `{slug}` is not installed."
        rc, out = _run_clawhub(["uninstall", slug], workdir=bot_root, timeout=30)
        if rc == 0:
            return f"🗑️ Uninstalled **{slug}**."
        return f"❌ Uninstall failed:\n```\n{out[:600]}\n```"

    # ── inspect ───────────────────────────────────────────────────────────────
    if op == "inspect":
        slug = cmd["slug"]
        rc, out = _run_clawhub(["inspect", slug], workdir=bot_root, timeout=30)
        if rc == 0:
            return f"🔍 **{slug}**\n```\n{out[:1200]}\n```"
        return f"❌ Could not inspect `{slug}`:\n```\n{out[:600]}\n```"

    # ── discover / search ─────────────────────────────────────────────────────
    if op == "discover":
        query = cmd.get("query", "").strip()
        if not query:
            # Show featured skills from Simmer API
            try:
                r = requests.get(
                    "https://api.simmer.markets/api/sdk/skills",
                    timeout=15,
                )
                r.raise_for_status()
                skills = r.json().get("skills", [])[:15]
                lines = ["**🌐 ClawHub skills (top 15):**"]
                for s in skills:
                    installed_tag = " ✅" if (bot_root / "skills" / s["id"]).exists() else ""
                    lines.append(
                        f"  • `{s['id']}`{installed_tag} — {s['description'][:80]}"
                    )
                lines.append("\nUse `discover <query>` to search, or `install skill <slug>` to install.")
                return "\n".join(lines)
            except Exception as e:
                return f"⚠️ Could not fetch skill list: {e}"
        # Search via clawhub CLI
        rc, out = _run_clawhub(["search", query], workdir=bot_root, timeout=30)
        lines = [f"**🔎 Results for `{query}`:**"]
        # Parse the table output: "slug  Name  (score)"
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("-"):
                continue
            parts = line.split("  ")
            if parts:
                slug = parts[0].strip()
                name = parts[1].strip() if len(parts) > 1 else slug
                installed_tag = " ✅" if (bot_root / "skills" / slug).exists() else ""
                lines.append(f"  • `{slug}`{installed_tag} — {name}")
        lines.append("\nUse `install skill <slug>` to install, `inspect skill <slug>` for details.")
        return "\n".join(lines[:25])

    # ── performance ──────────────────────────────────────────────────────────
    if op == "performance":
        api_key = os.environ.get("SIMMER_API_KEY", "")
        if not api_key:
            return "⚠️ No SIMMER_API_KEY in environment."
        briefing = _simmer_briefing(api_key)
        if not briefing:
            return "⚠️ Could not fetch Simmer briefing."

        lines = ["**📊 Skill performance (from Simmer briefing):**"]
        found_any = False
        for venue_name, venue_data in briefing.get("venues", {}).items():
            if not isinstance(venue_data, dict):
                continue
            by_skill = venue_data.get("by_skill", {})
            if not by_skill:
                continue
            found_any = True
            lines.append(f"\n__{venue_name}__")
            for skill_id, stats in by_skill.items():
                pnl = stats.get("pnl", 0)
                count = stats.get("positions_count", stats.get("count", "?"))
                pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
                lines.append(f"  • `{skill_id}` — {count} positions, P&L: {pnl_str}")

        if not found_any:
            lines.append("  No per-skill data in briefing yet.")
        return "\n".join(lines)

    return f"⚠️ Unknown ClawHub operation: {op}"
