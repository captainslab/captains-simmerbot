from __future__ import annotations

"""Discord bot — conversational AI + full control interface for btc-sprint-stack.

Natural language first: @mention or ? prefix routes to LLM which understands
intent and emits BOT_ACTION directives the bot executes automatically.

Explicit commands still work as shortcuts:
  !status, !pause, !resume, !params, !set, !skill, !cycle, !markets,
  !chart, !export, !logs, !restart, !stopall, !alert, !briefing, !help
"""

import asyncio
import csv
import io
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import discord
except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
    discord = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TUNABLE_KEYS = {
    'min_edge', 'min_confidence', 'max_slippage_pct',
    'cycle_interval_minutes', 'stop_loss_pct', 'take_profit_pct',
}

SKILL_LIBRARY   = Path.home() / 'captains-simmerbot' / 'skills'
SKILL_APPS_ROOT = Path.home() / 'apps'
SECRETS_FILE    = Path.home() / '.secrets' / 'simmer-btc-sprint-bot.env'
TMUX_SESSION    = 'simmerbot'
TMUX_MAIN_WIN   = 'btc-sprint-stack'   # window name of the main bot

HELP_TEXT = """**Captain Hook — Natural Language Bot**
Just @mention me or start with `?` and ask anything in plain English.

**What I can do:**
• Show status, PnL, positions, win rate
• Explain my last decision, why I skipped a trade
• Scan live BTC markets and tell you what looks good
• Trigger a trading cycle right now
• Show a PnL chart of recent trades
• Export your trade journal as CSV
• Set price or performance alerts
• Install, start, or stop skills
• Tail logs from any running skill
• Restart myself
• Give you a full morning briefing

**Slash shortcuts** (if you prefer):
`!status` `!pause` `!resume` `!params` `!set <k> <v>`
`!cycle` `!markets` `!chart` `!export` `!briefing`
`!skill list|install|status|stop <name>`
`!alert <condition>` (e.g. `!alert btc < 80000`)
`!logs [skill]` `!restart` `!stopall` `!help`"""

SYSTEM_PROMPT = """You are Captain Hook — the BTC Sprint Bot running on Simmer, betting on BTC price movements via Polymarket prediction markets.

Your personality: sharp, data-driven, direct. You think in edges, probabilities, and risk-adjusted returns. You can explain your own decisions clearly and act on instructions.

## Your trading strategy
- Trade BTC-related Polymarket markets (5m and 15m windows)
- Gate every trade: signal edge → regime filter → LLM validation → risk limits
- Self-learn via pending rule suggestions applied when evidence accumulates
- Hard limits: max_daily_loss, max_open_positions, max_single_market_exposure, max_trades_per_day

## Responding to the user
- Be direct and specific. Use data from the context provided.
- "Why did you skip?" → explain which gate blocked it (signal, regime, LLM, risk).
- "What looks good?" → analyze available markets from context and recommend.
- For param changes: suggest the value, tell them to confirm with `!set <key> <value>`.
- Keep responses under 400 words. Use Discord markdown (bold, code blocks).
- Never invent data not in the context.

## Executing actions
When the user asks you to DO something (not just explain), emit a BOT_ACTION directive on its own line at the END of your response. The bot will execute it and report back.

Supported actions:
  BOT_ACTION:cycle:          — trigger one trading cycle now
  BOT_ACTION:markets:        — fetch and show live BTC markets
  BOT_ACTION:chart:N         — ASCII PnL chart of last N trades (default 20)
  BOT_ACTION:export:N        — export last N trades as CSV (default 50)
  BOT_ACTION:briefing:       — full morning briefing
  BOT_ACTION:restart:        — restart the main bot process
  BOT_ACTION:stopall:        — stop all running skills
  BOT_ACTION:logs:NAME       — tail logs from skill NAME
  BOT_ACTION:skill_install:NAME  — install and launch skill NAME
  BOT_ACTION:skill_stop:NAME     — stop skill NAME
  BOT_ACTION:alert:TYPE:COND:VAL — set alert (TYPE=btc_price|win_rate, COND=lt|gt, VAL=number)

Example: user says "run a cycle now" → respond normally then add:
BOT_ACTION:cycle:

Example: user says "alert me if BTC drops below 80000" → respond then add:
BOT_ACTION:alert:btc_price:lt:80000

Only emit BOT_ACTION when the user clearly wants action taken, not just information.
"""

# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

_CONV_HISTORY: dict[int, deque] = {}
_CONV_MAX_TURNS = 8
_CONV_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

_ALERTS: list[dict] = []
_ALERTS_LOCK = threading.Lock()


def _require_discord() -> object:
    if discord is None:
        raise RuntimeError('discord.py is required for the Discord bot path')
    return discord


# ---------------------------------------------------------------------------
# BotState
# ---------------------------------------------------------------------------

class BotState:
    def __init__(self, live_params_path: Path, journal_path: Path | None = None):
        self.paused = False
        self.live_params_path = live_params_path
        self.journal_path = journal_path
        self._last_output: dict = {}
        self._lock = threading.Lock()
        # set by start_bot_thread so alert checker can send messages
        self._discord_channel_id: int | None = None
        self._discord_client: discord.Client | None = None

    def set_last_output(self, output: dict) -> None:
        with self._lock:
            self._last_output = output or {}

    def get_last_output(self) -> dict:
        with self._lock:
            return dict(self._last_output)

    def read_live_params(self) -> dict:
        try:
            return json.loads(self.live_params_path.read_text())
        except Exception:
            return {}

    def write_live_param(self, key: str, value: float) -> None:
        params = self.read_live_params()
        params[key] = value
        self.live_params_path.write_text(json.dumps(params, indent=2))

    def read_recent_journal(self, n: int = 50) -> list[dict]:
        if not self.journal_path or not self.journal_path.exists():
            return []
        try:
            lines = self.journal_path.read_text().strip().splitlines()
            rows = []
            for line in reversed(lines[-200:]):
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
                if len(rows) >= n:
                    break
            return list(reversed(rows))
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Skill manager
# ---------------------------------------------------------------------------

class SkillManager:
    def list_available(self) -> list[dict]:
        skills = []
        if not SKILL_LIBRARY.exists():
            return skills
        for d in sorted(SKILL_LIBRARY.iterdir()):
            if d.is_dir() and (d / 'main.py').exists():
                meta: dict = {}
                for fname in ('_meta.json', 'clawhub.json'):
                    f = d / fname
                    if f.exists():
                        try:
                            meta.update(json.loads(f.read_text()))
                        except Exception:
                            pass
                skills.append({'name': d.name, 'path': str(d), **meta})
        return skills

    def list_installed(self) -> list[dict]:
        installed = []
        if not SKILL_APPS_ROOT.exists():
            return installed
        for app_dir in sorted(SKILL_APPS_ROOT.iterdir()):
            skills_dir = app_dir / 'skills'
            if not skills_dir.exists():
                continue
            for skill_dir in sorted(skills_dir.iterdir()):
                if skill_dir.is_dir() and (skill_dir / 'main.py').exists():
                    installed.append({'name': skill_dir.name, 'app': app_dir.name, 'path': str(skill_dir)})
        return installed

    def tmux_windows(self) -> dict[str, str]:
        try:
            result = subprocess.run(
                ['tmux', 'list-windows', '-t', TMUX_SESSION, '-F', '#{window_name}:#{window_active}'],
                capture_output=True, text=True, timeout=5,
            )
            windows = {}
            for line in result.stdout.strip().splitlines():
                if ':' in line:
                    name, active = line.rsplit(':', 1)
                    windows[name] = 'active' if active == '1' else 'running'
            return windows
        except Exception:
            return {}

    def install(self, skill_name: str) -> tuple[bool, str]:
        src = SKILL_LIBRARY / skill_name
        if not src.exists():
            avail = ', '.join(s['name'] for s in self.list_available())
            return False, f'`{skill_name}` not found. Available: {avail}'
        if not (src / 'main.py').exists():
            return False, f'`{skill_name}` has no main.py — it\'s a reference skill, not a runnable bot.'
        app_dir = SKILL_APPS_ROOT / f'simmer-{skill_name}'
        dest = app_dir / 'skills' / skill_name
        try:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(str(src), str(dest))
        except Exception as exc:
            return False, f'Copy failed: {exc}'
        venv = app_dir / '.venv'
        if not venv.exists():
            btc_venv = SKILL_APPS_ROOT / 'simmer-btc-sprint-bot' / '.venv'
            if btc_venv.exists():
                try:
                    os.symlink(str(btc_venv), str(venv))
                except Exception:
                    pass
        python = venv / 'bin' / 'python' if venv.exists() else Path('python3')
        start_cmd = (
            f'cd {app_dir} && set -a && source {SECRETS_FILE} && set +a && '
            f'{python} skills/{skill_name}/main.py --loop --live'
        )
        try:
            subprocess.run(['tmux', 'new-window', '-t', TMUX_SESSION, '-n', skill_name, '-d', start_cmd],
                           check=True, timeout=10)
        except subprocess.CalledProcessError:
            try:
                subprocess.run(['tmux', 'respawn-window', '-t', f'{TMUX_SESSION}:{skill_name}', '-k', start_cmd],
                               check=True, timeout=10)
            except Exception as exc:
                return False, f'Installed but failed to launch: {exc}'
        return True, f'`{skill_name}` installed to `{app_dir}` and launched in tmux window `{skill_name}`.'

    def stop(self, skill_name: str) -> tuple[bool, str]:
        if skill_name not in self.tmux_windows():
            return False, f'`{skill_name}` is not running.'
        try:
            subprocess.run(['tmux', 'kill-window', '-t', f'{TMUX_SESSION}:{skill_name}'],
                           check=True, timeout=5)
            return True, f'`{skill_name}` stopped.'
        except Exception as exc:
            return False, f'Failed to stop: {exc}'


_SKILL_MGR = SkillManager()


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _action_cycle(state: BotState) -> str:
    """Trigger one trading cycle via subprocess."""
    secrets = str(SECRETS_FILE)
    bot_dir = str(SKILL_APPS_ROOT / 'simmer-btc-sprint-bot')
    python = str(SKILL_APPS_ROOT / 'simmer-btc-sprint-bot' / '.venv' / 'bin' / 'python')
    cmd = (
        f'cd {bot_dir} && set -a && source {secrets} && set +a && '
        f'{python} skills/btc-sprint-stack/main.py --once --live 2>&1 | tail -5'
    )
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        out = (result.stdout or result.stderr or '').strip()[-800:]
        return f'Cycle triggered.\n```\n{out}\n```' if out else 'Cycle triggered (no output).'
    except subprocess.TimeoutExpired:
        return 'Cycle timed out after 2 minutes.'
    except Exception as exc:
        return f'Cycle failed: {exc}'


def _action_markets(state: BotState) -> str:
    """Fetch live BTC fast markets via SimmerClient."""
    try:
        import sys
        venv_site = list((SKILL_APPS_ROOT / 'simmer-btc-sprint-bot' / '.venv').glob('lib/python*/site-packages'))
        if venv_site:
            sys.path.insert(0, str(venv_site[0]))
        from simmer_sdk import SimmerClient
        api_key = os.environ.get('SIMMER_API_KEY', '')
        if not api_key:
            return 'SIMMER_API_KEY not set.'
        client = SimmerClient(api_key=api_key, venue='polymarket', live=True)
        markets = client.get_fast_markets(asset='BTC', limit=10)
        if not markets:
            return 'No BTC fast markets found right now.'
        lines = ['**Live BTC Fast Markets:**']
        for m in markets[:8]:
            ctx = client.get_market_context(m.id)
            mkt = ctx.get('market', {}) if isinstance(ctx, dict) else {}
            prob = mkt.get('current_probability', '?')
            prob_str = f'{float(prob):.0%}' if isinstance(prob, (int, float)) else '?'
            resolves = (mkt.get('resolves_at') or '')[:16]
            q = (getattr(m, 'question', '') or '')[:70]
            lines.append(f'• **{prob_str}** — {q} _(resolves {resolves})_')
        return '\n'.join(lines)
    except Exception as exc:
        return f'Market scan failed: {exc}'


def _action_chart(state: BotState, n: int = 20) -> str:
    """ASCII sparkline of cumulative PnL across last N trades."""
    rows = state.read_recent_journal(n)
    if not rows:
        return 'No journal data yet.'
    pnls = []
    cumulative = 0.0
    for row in rows:
        outcome = row.get('outcome') or {}
        pnl = float(outcome.get('pnl', 0) if isinstance(outcome, dict) else 0)
        cumulative += pnl
        pnls.append(cumulative)
    if not pnls:
        return 'No PnL data in journal.'
    mn, mx = min(pnls), max(pnls)
    blocks = '▁▂▃▄▅▆▇█'
    if mx == mn:
        bar = blocks[3] * len(pnls)
    else:
        bar = ''.join(blocks[int((v - mn) / (mx - mn) * 7)] for v in pnls)
    final = pnls[-1]
    wins = sum(1 for row in rows if (row.get('outcome') or {}).get('pnl', 0) > 0 if isinstance(row.get('outcome'), dict))
    win_rate = wins / len(rows) if rows else 0
    return (
        f'**PnL chart** (last {len(pnls)} trades)\n'
        f'`{bar}`\n'
        f'Cumulative: **${final:+.2f}** | Win rate: **{win_rate:.0%}** | '
        f'Range: ${mn:.2f} → ${mx:.2f}'
    )


def _action_export(state: BotState, n: int = 50) -> discord.File | str:
    """Generate CSV of last N trades and return as discord.File."""
    if discord is None:
        return 'Discord export is unavailable because discord.py is not installed.'
    rows = state.read_recent_journal(n)
    if not rows:
        return 'No journal data to export.'
    fields = ['ts', 'market_id', 'question', 'window', 'execution_status',
              'signal_action', 'edge', 'confidence', 'reject_reason', 'pnl']
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        sig = row.get('signal_data') or {}
        outcome = row.get('outcome') or {}
        writer.writerow({
            'ts': row.get('ts', ''),
            'market_id': row.get('market_id', ''),
            'question': (row.get('question') or '')[:80],
            'window': row.get('window', ''),
            'execution_status': row.get('execution_status', ''),
            'signal_action': row.get('signal_action', ''),
            'edge': sig.get('edge', '') if isinstance(sig, dict) else '',
            'confidence': sig.get('confidence', '') if isinstance(sig, dict) else '',
            'reject_reason': row.get('reject_reason', ''),
            'pnl': outcome.get('pnl', '') if isinstance(outcome, dict) else '',
        })
    buf.seek(0)
    fname = f'trades_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")}.csv'
    return discord.File(io.BytesIO(buf.read().encode()), filename=fname)


def _action_briefing(state: BotState) -> str:
    """Build a rich morning briefing string (passed back to LLM for formatting)."""
    return _build_bot_context(state)  # LLM will format it as a briefing


def _action_restart(state: BotState) -> str:
    """Restart the main bot process in its tmux window."""
    start_cmd = (
        f'cd {SKILL_APPS_ROOT}/simmer-btc-sprint-bot && '
        f'set -a && source {SECRETS_FILE} && set +a && '
        f'.venv/bin/python skills/btc-sprint-stack/main.py --loop --live'
    )
    try:
        subprocess.run(
            ['tmux', 'respawn-window', '-t', f'{TMUX_SESSION}:{TMUX_MAIN_WIN}', '-k', start_cmd],
            check=True, timeout=10,
        )
        return 'Bot restarting in tmux — give it ~10 seconds.'
    except Exception as exc:
        return f'Restart failed: {exc}'


def _action_stopall(state: BotState) -> str:
    """Kill all tmux windows except the current one."""
    windows = _SKILL_MGR.tmux_windows()
    stopped = []
    for name in list(windows.keys()):
        if name == TMUX_MAIN_WIN:
            continue
        try:
            subprocess.run(['tmux', 'kill-window', '-t', f'{TMUX_SESSION}:{name}'],
                           check=True, timeout=5)
            stopped.append(name)
        except Exception:
            pass
    state.paused = True
    return f'Paused trading + stopped: {", ".join(stopped) or "nothing running"}.'


def _action_logs(skill_name: str = '') -> str:
    """Tail last 30 lines from a tmux window."""
    target = skill_name.strip() or TMUX_MAIN_WIN
    try:
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', f'{TMUX_SESSION}:{target}', '-p'],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l for l in result.stdout.splitlines() if l.strip()][-30:]
        if not lines:
            return f'No output from `{target}`.'
        return f'**Logs: {target}**\n```\n' + '\n'.join(lines) + '\n```'
    except Exception as exc:
        return f'Could not get logs: {exc}'


def _set_alert(alert_type: str, condition: str, value: str) -> str:
    """Register a new alert."""
    try:
        val = float(value)
    except ValueError:
        return f'Invalid alert value: `{value}`'
    with _ALERTS_LOCK:
        _ALERTS.append({'type': alert_type, 'condition': condition, 'value': val, 'triggered': False})
    cond_str = '<' if condition == 'lt' else '>'
    label = {'btc_price': 'BTC price', 'win_rate': 'win rate'}.get(alert_type, alert_type)
    return f'Alert set: ping when {label} {cond_str} {val}'


def _check_alerts(state: BotState) -> list[str]:
    """Check active alerts, return messages for any that triggered."""
    triggered = []
    output = state.get_last_output()
    hb = output.get('heartbeat', {}) if output else {}
    perf = hb.get('performance', {}) if hb else {}

    # Get BTC price from Binance
    btc_price = None
    try:
        req = Request(
            'https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT',
            headers={'User-Agent': 'simmer-btc-sprint-bot/1.0'},
        )
        with urlopen(req, timeout=5) as resp:
            btc_price = float(json.loads(resp.read())['price'])
    except Exception:
        pass

    win_rate = perf.get('win_rate')

    with _ALERTS_LOCK:
        for alert in _ALERTS:
            if alert['triggered']:
                continue
            val = alert['value']
            cond = alert['condition']
            atype = alert['type']

            hit = False
            current = None
            if atype == 'btc_price' and btc_price is not None:
                current = btc_price
                hit = (cond == 'lt' and btc_price < val) or (cond == 'gt' and btc_price > val)
            elif atype == 'win_rate' and win_rate is not None:
                current = win_rate
                hit = (cond == 'lt' and win_rate < val) or (cond == 'gt' and win_rate > val)

            if hit:
                alert['triggered'] = True
                cond_str = '<' if cond == 'lt' else '>'
                label = {'btc_price': 'BTC price', 'win_rate': 'win rate'}.get(atype, atype)
                triggered.append(
                    f'🚨 **Alert triggered:** {label} {cond_str} {val} '
                    f'(current: {current:.2f})'
                )
    return triggered


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _get_chat_creds() -> tuple[str, str, str] | None:
    provider = os.environ.get('LLM_PROVIDER', '').strip().lower()
    api_key = os.environ.get('LLM_API_KEY', '').strip()
    model = os.environ.get('LLM_MODEL', '').strip()
    if not api_key:
        return None
    if provider == 'openrouter':
        base_url = 'https://openrouter.ai/api/v1'
        model = model or 'google/gemini-2.5-pro'
    elif provider == 'openai':
        base_url = 'https://api.openai.com/v1'
        model = model or 'gpt-4o-mini'
    elif provider == 'google':
        base_url = 'https://generativelanguage.googleapis.com/v1beta/openai/'
        model = model or 'gemini-2.5-flash'
    else:
        base_url = os.environ.get('LLM_BASE_URL', 'https://openrouter.ai/api/v1').strip()
    return api_key, base_url, model


def _chat_complete(messages: list[dict], *, api_key: str, base_url: str, model: str, timeout: float = 45.0) -> str:
    body = json.dumps({'model': model, 'max_tokens': 800, 'messages': messages}).encode()
    req = Request(
        f'{base_url.rstrip("/")}/chat/completions',
        data=body,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'User-Agent': 'DiscordBot (simmer-btc-sprint-bot, 1.0)',
        },
        method='POST',
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except HTTPError as exc:
        raise RuntimeError(f'LLM HTTP {exc.code}') from exc
    except URLError as exc:
        raise RuntimeError(f'LLM request failed: {exc.reason}') from exc
    return json.loads(raw)['choices'][0]['message']['content'].strip()


def _get_or_create_history(channel_id: int) -> deque:
    with _CONV_LOCK:
        if channel_id not in _CONV_HISTORY:
            _CONV_HISTORY[channel_id] = deque(maxlen=_CONV_MAX_TURNS * 2)
        return _CONV_HISTORY[channel_id]


def _append_history(channel_id: int, role: str, content: str) -> None:
    h = _get_or_create_history(channel_id)
    with _CONV_LOCK:
        h.append({'role': role, 'content': content})


def _build_messages(channel_id: int, user_message: str, context: str) -> list[dict]:
    h = _get_or_create_history(channel_id)
    msgs = [{'role': 'system', 'content': SYSTEM_PROMPT + '\n\n--- CURRENT BOT STATE ---\n' + context}]
    with _CONV_LOCK:
        msgs.extend(list(h))
    msgs.append({'role': 'user', 'content': user_message})
    return msgs


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def _build_bot_context(state: BotState) -> str:
    output = state.get_last_output()
    parts = [f"**Bot status:** {'PAUSED' if state.paused else 'running'}"]

    if output:
        hb = output.get('heartbeat') or {}
        perf = hb.get('performance') or {}
        risk = output.get('risk_state') or {}
        live_params = output.get('live_params') or state.read_live_params()
        learning = output.get('learning_snapshot') or {}
        pending_rules = output.get('pending_rules') or {}
        llm_info = hb.get('llm') or {}

        pnl = perf.get('total_pnl', 0)
        win_rate = perf.get('win_rate', 0)
        parts.append(f"**Performance:** PnL ${pnl:.2f} ({perf.get('pnl_percent', 0):.1f}%) | Win rate {win_rate:.0%} | Rank {perf.get('rank', '?')}")
        parts.append(f"**Risk:** {risk.get('open_positions', '?')} open positions | ${risk.get('daily_spent', 0):.2f} spent today")

        if live_params:
            pstr = ' | '.join(f'{k}: {v}' for k, v in sorted(live_params.items()) if k in TUNABLE_KEYS)
            if pstr:
                parts.append(f"**Live params:** {pstr}")

        if llm_info:
            sc = llm_info.get('status_counts') or {}
            parts.append(f"**LLM:** {llm_info.get('provider')}/{llm_info.get('model')} | validated:{sc.get('validated',0)} rejected:{sc.get('rejected',0)}")

        trade_count = learning.get('trade_count', 0)
        if trade_count:
            parts.append(f"**Learning:** {trade_count} trades in history")

        rules = (pending_rules.get('rules') or []) if isinstance(pending_rules, dict) else []
        if rules:
            parts.append("**Pending rules:**\n" + '\n'.join(f"  - {r.get('key')}={r.get('value')}: {r.get('why','')[:80]}" for r in rules[-3:]))

        decisions = output.get('decisions') or []
        if decisions:
            last = decisions[-1]
            sig = last.get('signal_data') or {}
            llm_dec = last.get('validated_llm_decision') or {}
            parts.append(
                f"**Last decision:** {last.get('execution_status','?')} | {(last.get('question') or '')[:70]}\n"
                f"  Edge:{sig.get('edge','?')} Conf:{sig.get('confidence','?')}"
                + (f"\n  Reject: {last.get('reject_reason')}" if last.get('reject_reason') else '')
                + (f"\n  LLM: {(llm_dec.get('reasoning') or '')[:150]}" if llm_dec.get('reasoning') else '')
            )
    else:
        parts.append("No cycle data yet.")

    recent = state.read_recent_journal(5)
    if recent:
        parts.append("**Recent journal:**\n" + '\n'.join(
            f"  {(r.get('ts') or '')[:16]} [{r.get('execution_status','?')}] {(r.get('question') or r.get('market_id',''))[:55]}"
            for r in recent
        ))

    available = _SKILL_MGR.list_available()
    running = _SKILL_MGR.tmux_windows()
    if available:
        parts.append("**Skills:**\n" + '\n'.join(
            f"  {s['name']} v{s.get('version','?')} — {'🟢 running' if s['name'] in running else '⚫ stopped'}"
            for s in available
        ))

    with _ALERTS_LOCK:
        active = [a for a in _ALERTS if not a['triggered']]
    if active:
        parts.append("**Active alerts:** " + ', '.join(
            f"{a['type']} {'<' if a['condition']=='lt' else '>'} {a['value']}" for a in active
        ))

    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Action dispatcher (parses BOT_ACTION lines from LLM reply)
# ---------------------------------------------------------------------------

async def _dispatch_action(line: str, state: BotState, channel: discord.TextChannel) -> None:
    """Parse and execute a BOT_ACTION directive."""
    parts = line.strip().split(':', 2)
    if len(parts) < 2:
        return
    action = parts[1].lower().strip()
    arg = parts[2].strip() if len(parts) > 2 else ''

    if action == 'cycle':
        await channel.send('Running a cycle...')
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: _action_cycle(state))
        await channel.send(result[:1900])

    elif action == 'markets':
        await channel.send('Scanning markets...')
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: _action_markets(state))
        await channel.send(result[:1900])

    elif action == 'chart':
        n = int(arg) if arg.isdigit() else 20
        result = _action_chart(state, n)
        await channel.send(result[:1900])

    elif action == 'export':
        n = int(arg) if arg.isdigit() else 50
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: _action_export(state, n))
        if isinstance(result, discord.File):
            await channel.send(f'Here are your last {n} trades:', file=result)
        else:
            await channel.send(result)

    elif action == 'briefing':
        creds = _get_chat_creds()
        if not creds:
            await channel.send('LLM not configured for briefing.')
            return
        api_key, base_url, model = creds
        ctx = _action_briefing(state)
        msgs = [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': f'Give me a full morning briefing based on this data:\n\n{ctx}'},
        ]
        async with channel.typing():
            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(None, lambda: _chat_complete(msgs, api_key=api_key, base_url=base_url, model=model))
        await channel.send(reply[:1900])

    elif action == 'restart':
        result = _action_restart(state)
        await channel.send(result)

    elif action == 'stopall':
        result = _action_stopall(state)
        await channel.send(result)

    elif action == 'logs':
        result = _action_logs(arg)
        await channel.send(result[:1900])

    elif action == 'skill_install':
        ok, msg = _SKILL_MGR.install(arg)
        await channel.send(('✅ ' if ok else '❌ ') + msg)

    elif action == 'skill_stop':
        ok, msg = _SKILL_MGR.stop(arg)
        await channel.send(('✅ ' if ok else '❌ ') + msg)

    elif action == 'alert':
        sub = arg.split(':')
        if len(sub) >= 3:
            result = _set_alert(sub[0], sub[1], sub[2])
            await channel.send(result)
        else:
            await channel.send('Alert format: `BOT_ACTION:alert:TYPE:COND:VALUE`')


# ---------------------------------------------------------------------------
# Alert monitor thread
# ---------------------------------------------------------------------------

def _alert_monitor(state: BotState) -> None:
    """Background thread: check alerts every 60s and push to Discord."""
    while True:
        time.sleep(60)
        try:
            triggered = _check_alerts(state)
            if triggered and state._discord_client and state._discord_channel_id:
                channel = state._discord_client.get_channel(state._discord_channel_id)
                if channel:
                    for msg in triggered:
                        asyncio.run_coroutine_threadsafe(channel.send(msg), state._discord_client.loop)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------

def _make_client(state: BotState) -> discord.Client:
    _require_discord()
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f'[discord_bot] logged in as {client.user}', flush=True)
        state._discord_client = client

    @client.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return
        content = message.content.strip()
        if not content:
            return

        # Track channel for alert pushes
        state._discord_channel_id = message.channel.id

        is_mentioned = client.user in message.mentions
        is_question  = content.startswith('?')
        is_command   = content.startswith('!')

        # ------------------------------------------------------------------
        # Explicit commands
        # ------------------------------------------------------------------
        if is_command:
            cmd_parts = content.split()
            cmd = cmd_parts[0].lower()

            if cmd == '!help':
                await message.channel.send(HELP_TEXT)

            elif cmd == '!status':
                output = state.get_last_output()
                if not output:
                    await message.channel.send('No cycle data yet.')
                    return
                hb = output.get('heartbeat') or {}
                perf = hb.get('performance') or {}
                risk = output.get('risk_state') or {}
                llm = hb.get('llm') or {}
                paused_str = ' | **PAUSED**' if state.paused else ''
                await message.channel.send(
                    f'**BTC Sprint Bot{paused_str}**\n'
                    f'PnL: ${perf.get("total_pnl",0):.2f} ({perf.get("pnl_percent",0):.1f}%) | '
                    f'Win rate: {perf.get("win_rate",0):.0%} | Rank: {perf.get("rank","?")}\n'
                    f'Open: {risk.get("open_positions","?")} | Daily spent: ${risk.get("daily_spent",0):.2f}\n'
                    f'LLM: {llm.get("provider","?")} / {llm.get("model","?")}'
                )

            elif cmd == '!pause':
                state.paused = True
                await message.channel.send('Trading **paused**.')

            elif cmd == '!resume':
                state.paused = False
                await message.channel.send('Trading **resumed**.')

            elif cmd == '!params':
                params = state.read_live_params()
                if not params:
                    await message.channel.send('No live params (using defaults).')
                    return
                await message.channel.send('**Live Params**\n' + '\n'.join(f'`{k}`: {v}' for k, v in sorted(params.items())))

            elif cmd == '!set':
                if len(cmd_parts) < 3:
                    await message.channel.send('Usage: `!set <key> <value>`')
                    return
                key = cmd_parts[1].lower()
                if key not in TUNABLE_KEYS:
                    await message.channel.send(f'Unknown key. Valid: {", ".join(sorted(TUNABLE_KEYS))}')
                    return
                try:
                    value = float(cmd_parts[2])
                except ValueError:
                    await message.channel.send(f'Value must be a number.')
                    return
                state.write_live_param(key, value)
                await message.channel.send(f'Set `{key}` = `{value}` — takes effect next cycle.')

            elif cmd == '!cycle':
                await message.channel.send('Running a cycle...')
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: _action_cycle(state))
                await message.channel.send(result[:1900])

            elif cmd == '!markets':
                await message.channel.send('Scanning markets...')
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: _action_markets(state))
                await message.channel.send(result[:1900])

            elif cmd == '!chart':
                n = int(cmd_parts[1]) if len(cmd_parts) > 1 and cmd_parts[1].isdigit() else 20
                await message.channel.send(_action_chart(state, n))

            elif cmd == '!export':
                n = int(cmd_parts[1]) if len(cmd_parts) > 1 and cmd_parts[1].isdigit() else 50
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: _action_export(state, n))
                if isinstance(result, discord.File):
                    await message.channel.send(f'Last {n} trades:', file=result)
                else:
                    await message.channel.send(result)

            elif cmd == '!briefing':
                await _dispatch_action('BOT_ACTION:briefing:', state, message.channel)

            elif cmd == '!logs':
                skill_name = cmd_parts[1] if len(cmd_parts) > 1 else ''
                await message.channel.send(_action_logs(skill_name)[:1900])

            elif cmd == '!restart':
                await message.channel.send(_action_restart(state))

            elif cmd == '!stopall':
                await message.channel.send(_action_stopall(state))

            elif cmd == '!alert':
                # !alert btc < 80000  or  !alert winrate < 0.4
                rest = ' '.join(cmd_parts[1:]).lower().replace('btc_price', 'btc').replace('win_rate', 'winrate')
                if 'btc' in rest:
                    atype = 'btc_price'
                elif 'win' in rest:
                    atype = 'win_rate'
                else:
                    await message.channel.send('Usage: `!alert btc < 80000` or `!alert winrate < 40%`')
                    return
                cond = 'lt' if '<' in rest else 'gt'
                nums = [p for p in cmd_parts[2:] if p.replace('.', '').replace('%', '').isdigit()]
                if not nums:
                    await message.channel.send('Could not parse value.')
                    return
                val = nums[0].rstrip('%')
                if atype == 'win_rate' and float(val) > 1:
                    val = str(float(val) / 100)
                await message.channel.send(_set_alert(atype, cond, val))

            elif cmd == '!skill':
                sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else 'help'
                if sub == 'list':
                    avail = _SKILL_MGR.list_available()
                    lines = '\n'.join(f'  `{s["name"]}` v{s.get("version","?")}' for s in avail) or 'None found.'
                    await message.channel.send(f'**Available skills:**\n{lines}')
                elif sub == 'status':
                    windows = _SKILL_MGR.tmux_windows()
                    installed = _SKILL_MGR.list_installed()
                    lines = '\n'.join(
                        f'  `{s["name"]}` — {"🟢 running" if s["name"] in windows else "⚫ stopped"}'
                        for s in installed
                    ) or 'None installed.'
                    await message.channel.send(f'**Installed skills:**\n{lines}')
                elif sub == 'install' and len(cmd_parts) > 2:
                    ok, msg = _SKILL_MGR.install(cmd_parts[2])
                    await message.channel.send(('✅ ' if ok else '❌ ') + msg)
                elif sub == 'stop' and len(cmd_parts) > 2:
                    ok, msg = _SKILL_MGR.stop(cmd_parts[2])
                    await message.channel.send(('✅ ' if ok else '❌ ') + msg)
                else:
                    await message.channel.send(HELP_TEXT)

        # ------------------------------------------------------------------
        # Conversational AI
        # ------------------------------------------------------------------
        elif is_mentioned or is_question:
            user_text = content
            if is_mentioned and client.user:
                user_text = user_text.replace(f'<@{client.user.id}>', '').replace(f'<@!{client.user.id}>', '').strip()
            if user_text.startswith('?'):
                user_text = user_text[1:].strip()
            if not user_text:
                user_text = 'Give me a status summary.'

            creds = _get_chat_creds()
            if not creds:
                await message.channel.send('LLM not configured — set `LLM_API_KEY`.')
                return

            api_key, base_url, model = creds
            ctx = _build_bot_context(state)
            msgs = _build_messages(message.channel.id, user_text, ctx)

            async with message.channel.typing():
                try:
                    loop = asyncio.get_event_loop()
                    reply = await loop.run_in_executor(
                        None, lambda: _chat_complete(msgs, api_key=api_key, base_url=base_url, model=model)
                    )
                except Exception as exc:
                    await message.channel.send(f'Chat error: `{exc}`')
                    return

            # Extract and strip BOT_ACTION lines
            action_lines = [l for l in reply.splitlines() if l.strip().startswith('BOT_ACTION:')]
            clean_reply = '\n'.join(l for l in reply.splitlines() if not l.strip().startswith('BOT_ACTION:')).strip()

            _append_history(message.channel.id, 'user', user_text)
            _append_history(message.channel.id, 'assistant', clean_reply or reply)

            if clean_reply:
                for chunk in [clean_reply[i:i+1900] for i in range(0, len(clean_reply), 1900)]:
                    await message.channel.send(chunk)

            # Execute any actions the LLM requested
            for action_line in action_lines:
                await _dispatch_action(action_line, state, message.channel)

    return client


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def start_bot_thread(state: BotState) -> threading.Thread | None:
    token = os.environ.get('DISCORD_BOT_TOKEN', '').strip()
    if not token:
        print('[discord_bot] DISCORD_BOT_TOKEN not set — bot disabled', flush=True)
        return None
    if discord is None:
        print('[discord_bot] discord.py not installed — bot disabled', flush=True)
        return None

    # Start alert monitor
    t_alerts = threading.Thread(target=_alert_monitor, args=(state,), name='alert-monitor', daemon=True)
    t_alerts.start()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = _make_client(state)
        try:
            loop.run_until_complete(client.start(token))
        except Exception as exc:
            print(f'[discord_bot] error: {exc}', flush=True)
        finally:
            loop.close()

    t = threading.Thread(target=_run, name='discord-bot', daemon=True)
    t.start()
    return t
