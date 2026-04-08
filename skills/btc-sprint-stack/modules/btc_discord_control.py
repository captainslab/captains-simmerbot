from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_CONTROL_STATE = {
    'execution_profile': None,
    'strategy_label': None,
    'skill_tags': [],
    'live_overrides': {},
    'trading_paused': False,
    'updated_at': None,
    'last_command': None,
    'last_user_id': None,
    'last_channel_id': None,
}

CONTROL_PREFIXES = ('simmer:', '!simmer', '/simmer')
FLOAT_KEYS = {
    'min_edge',
    'min_confidence',
    'max_slippage_pct',
    'stop_loss_pct',
    'take_profit_pct',
    'max_trade_usd',
    'max_daily_loss_usd',
    'max_single_market_exposure_usd',
    'bankroll_usd',
}
INT_KEYS = {
    'cycle_interval_minutes',
    'max_open_positions',
    'max_trades_per_day',
    'cooldown_after_loss_minutes',
}

_PAUSE_PATTERN = re.compile(
    r'\b(pause\s+(?:trading|bot|all\s+trades?)|stop\s+trading|halt\s+trading|trading\s+(?:off|pause))\b'
)
_RESUME_PATTERN = re.compile(
    r'\b(resume\s+(?:trading|bot|all\s+trades?)|start\s+trading|enable\s+trading|unpause\s+(?:trading|bot)|trading\s+(?:on|resume))\b'
)


@dataclass
class ControlUpdate:
    execution_profile: str | None = None
    strategy_label: str | None = None
    skill_tags: list[str] = field(default_factory=list)
    replace_skill_tags: bool = False
    remove_skill_tags: list[str] = field(default_factory=list)
    clear_skill_tags: bool = False
    live_overrides: dict[str, Any] = field(default_factory=dict)
    clear_live_overrides: bool = False
    reset_strategy: bool = False
    trading_paused: bool | None = None
    summary: str = ''


def current_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_skill_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r'[,/]| and ', value, flags=re.IGNORECASE)
        return [item.strip() for item in raw_items if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def load_control_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return dict(DEFAULT_CONTROL_STATE)
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return dict(DEFAULT_CONTROL_STATE)
    if not isinstance(payload, dict):
        return dict(DEFAULT_CONTROL_STATE)
    state = dict(DEFAULT_CONTROL_STATE)
    state['execution_profile'] = _normalize_profile(payload.get('execution_profile')) or state['execution_profile']
    strategy_label = payload.get('strategy_label')
    if isinstance(strategy_label, str) and strategy_label.strip():
        state['strategy_label'] = strategy_label.strip()
    state['skill_tags'] = _coerce_skill_tags(payload.get('skill_tags'))
    live_overrides = payload.get('live_overrides')
    state['live_overrides'] = live_overrides if isinstance(live_overrides, dict) else {}
    trading_paused = payload.get('trading_paused')
    if isinstance(trading_paused, bool):
        state['trading_paused'] = trading_paused
    for key in ('updated_at', 'last_command', 'last_user_id', 'last_channel_id'):
        value = payload.get(key)
        if value is None:
            continue
        state[key] = value
    return state


def write_control_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(DEFAULT_CONTROL_STATE)
    payload.update(state)
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp_path, path)


def _strip_optional_control_prefix(message: str, prefixes: Sequence[str] = CONTROL_PREFIXES) -> str:
    text = message.strip()
    lowered = text.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _normalize_profile(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {'balanced', 'default', 'normal', 'steady', 'conservative', 'safe'}:
        return 'balanced'
    if normalized in {'aggressive', 'fast', 'active', 'loose'}:
        return 'aggressive'
    return None


def _parse_ratio_value(raw: str) -> float | None:
    text = raw.strip().lower().replace('percent', '%')
    if not text:
        return None
    percent = text.endswith('%')
    if percent:
        text = text[:-1].strip()
    try:
        value = float(text)
    except ValueError:
        return None
    if value > 1.0 or percent:
        return value / 100.0
    return value


def _parse_minutes_value(raw: str) -> int | None:
    text = raw.strip().lower()
    if not text:
        return None
    text = re.sub(r'\b(minutes?|mins?|m)\b', '', text).strip()
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_money_value(raw: str) -> float | None:
    text = raw.strip().lower().replace(',', '')
    if not text:
        return None
    text = text.replace('$', '')
    text = re.sub(r'\b(?:usd|dollars?|bucks?)\b', '', text).strip()
    try:
        return float(text)
    except ValueError:
        return None


def _parse_integer_value(raw: str) -> int | None:
    text = raw.strip().lower().replace(',', '')
    if not text:
        return None
    text = re.sub(r'\b(?:positions?|trades?|slots?|open|per|day|minutes?|mins?|m)\b', '', text).strip()
    try:
        return int(float(text))
    except ValueError:
        return None


def _extract_profile(text: str) -> str | None:
    lowered = text.lower()
    if re.search(r'\b(reset|default|go back to default|back to balanced|restore defaults)\b', lowered):
        return 'balanced'
    if re.search(r'\b(more aggressive|be aggressive|go aggressive|speed up|take more trades|loosen risk|loosen up|trade more often|be bolder|bump risk)\b', lowered):
        return 'aggressive'
    if re.search(r'\b(more conservative|be conservative|go conservative|tighten risk|slow down|safer|reduce risk|be more selective|trade less often|tighten up|pull risk back)\b', lowered):
        return 'balanced'
    explicit = re.search(r'\b(?:set|switch|use|move|change|make)\s+(?:the\s+)?(?:profile|mode|strategy)\s*(?:to|=)?\s*(aggressive|balanced|conservative|default|normal|safe)\b', lowered)
    if explicit:
        return _normalize_profile(explicit.group(1))
    return None


def _extract_label(text: str) -> str | None:
    patterns = [
        r'\b(?:set|switch|use|rename|change|call|name)\s+(?:the\s+)?(?:strategy|skill|bot|mode)?\s*(?:label|name|tag)?\s*(?:to|as|=)?\s+(.+)$',
        r'\b(?:call|name)\s+(?:it|this)\s+(.+)$',
        r'\b(?:strategy|skill)\s*(?:label|name|tag)\s*(?:to|=|is)?\s+(.+)$',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            label = match.group(1).strip()
            label = re.split(r'\b(?:and|plus|with)\b', label, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            label = label.rstrip(' .!,')
            if label:
                return label
    return None


def _extract_skill_tags(text: str) -> tuple[list[str], bool, bool, bool]:
    lowered = text.lower()
    if re.search(r'\b(clear|reset|remove all|wipe)\s+(?:skill|skills|tags)\b', lowered):
        return [], False, True, False
    add_match = re.search(r'\b(?:add|append|include|enable|use)\s+(?:skill|skills|tag|tags)?\s*(?:to|:)?\s+(.+)$', text, flags=re.IGNORECASE)
    if add_match:
        return _coerce_skill_tags(add_match.group(1)), True, False, False
    set_match = re.search(r'\b(?:set|replace|make)\s+(?:skill|skills|tag|tags)\s*(?:to|=)?\s+(.+)$', text, flags=re.IGNORECASE)
    if set_match:
        return _coerce_skill_tags(set_match.group(1)), False, False, False
    remove_match = re.search(r'\b(?:remove|drop|disable|stop using)\s+(?:skill|skills|tag|tags)\s*(?:from|:)?\s+(.+)$', text, flags=re.IGNORECASE)
    if remove_match:
        return _coerce_skill_tags(remove_match.group(1)), False, False, True
    inline_add_match = re.search(r'\b(?:add|append|include|enable|use)\s+(.+?)\s+(?:skill|skills|tag|tags)\b', text, flags=re.IGNORECASE)
    if inline_add_match:
        return _coerce_skill_tags(inline_add_match.group(1)), True, False, False
    inline_remove_match = re.search(r'\b(?:remove|drop|disable|stop using)\s+(.+?)\s+(?:skill|skills|tag|tags)\b', text, flags=re.IGNORECASE)
    if inline_remove_match:
        return _coerce_skill_tags(inline_remove_match.group(1)), False, False, True
    if re.search(r'\b(?:skill|skills)\s*:\s*', lowered):
        _, _, tail = lowered.partition(':')
        return _coerce_skill_tags(tail), False, False, False
    return [], False, False, False


def _extract_numeric_overrides(text: str) -> dict[str, float | int]:
    lowered = text.lower()
    overrides: dict[str, float | int] = {}
    patterns: dict[str, list[tuple[str, str]]] = {
        'min_edge': [
            (r'\bmin(?:imum)?\s*edge(?:\s*threshold)?\b(?:\s*(?:to|=)\s*|\s+)([0-9.]+%?)', 'ratio'),
            (r'\bedge(?:\s*threshold)?\b(?:\s*(?:to|=)\s*|\s+)([0-9.]+%?)', 'ratio'),
        ],
        'min_confidence': [
            (r'\bmin(?:imum)?\s*confidence\b(?:\s*(?:to|=)\s*|\s+)([0-9.]+%?)', 'ratio'),
            (r'\bconfidence\b(?:\s*(?:to|=)\s*|\s+)([0-9.]+%?)', 'ratio'),
        ],
        'max_slippage_pct': [
            (r'\bmax(?:imum)?\s*slippage\b(?:\s*(?:to|=)\s*|\s+)([0-9.]+%?)', 'ratio'),
            (r'\bslippage\b(?:\s*(?:to|=)\s*|\s+)([0-9.]+%?)', 'ratio'),
        ],
        'stop_loss_pct': [
            (r'\bstop\s*loss\b(?:\s*(?:to|=)\s*|\s+)([0-9.]+%?)', 'ratio'),
        ],
        'take_profit_pct': [
            (r'\btake\s*profit\b(?:\s*(?:to|=)\s*|\s+)([0-9.]+%?)', 'ratio'),
        ],
        'max_trade_usd': [
            (r'\bmax(?:imum)?\s*trade(?:\s*(?:size|amount))?(?:\s*(?:to|=)\s*|\s+)([$0-9.,]+\s*(?:usd|dollars?|bucks?)?)', 'money'),
            (r'\btrade(?:\s*(?:size|amount))?(?:\s*(?:to|=)\s*|\s+)([$0-9.,]+\s*(?:usd|dollars?|bucks?)?)', 'money'),
        ],
        'max_daily_loss_usd': [
            (r'\bmax(?:imum)?\s*daily\s*loss(?:\s*limit)?(?:\s*(?:to|=)\s*|\s+)([$0-9.,]+\s*(?:usd|dollars?|bucks?)?)', 'money'),
            (r'\bdaily\s*loss(?:\s*limit)?(?:\s*(?:to|=)\s*|\s+)([$0-9.,]+\s*(?:usd|dollars?|bucks?)?)', 'money'),
        ],
        'max_single_market_exposure_usd': [
            (r'\b(?:max(?:imum)?\s*)?(?:single\s*market\s*exposure|market\s*exposure)(?:\s*(?:to|=)\s*|\s+)([$0-9.,]+\s*(?:usd|dollars?|bucks?)?)', 'money'),
        ],
        'bankroll_usd': [
            (r'\bbankroll(?:\s*(?:to|=)\s*|\s+)([$0-9.,]+\s*(?:usd|dollars?|bucks?)?)', 'money'),
        ],
        'max_open_positions': [
            (r'\b(?:allow|permit|let(?:\s+me)?\s+have)\s+(\d+)\s+open\s*positions?\b', 'int'),
            (r'\b(?:set|limit|cap)\s+open\s*positions?(?:\s*(?:to|=)\s*|\s+)(\d+)', 'int'),
            (r'\bmax(?:imum)?\s*open\s*positions?(?:\s*(?:to|=)\s*|\s+)(\d+)', 'int'),
            (r'\bopen\s*positions?(?:\s*(?:to|=)\s*|\s+)(\d+)', 'int'),
        ],
        'max_trades_per_day': [
            (r'\b(?:allow|permit|let(?:\s+me)?\s+have)\s+(\d+)\s+trades?\s*per\s*day\b', 'int'),
            (r'\b(?:set|limit|cap)\s+trades?\s*per\s*day(?:\s*(?:to|=)\s*|\s+)(\d+)', 'int'),
            (r'\bmax(?:imum)?\s*trades?\s*per\s*day(?:\s*(?:to|=)\s*|\s+)(\d+)', 'int'),
            (r'\btrades?\s*per\s*day(?:\s*(?:to|=)\s*|\s+)(\d+)', 'int'),
        ],
        'cooldown_after_loss_minutes': [
            (r'\bcooldown(?:\s*after\s*loss)?(?:\s*(?:to|=)\s*|\s+)([0-9.]+\s*(?:minutes?|mins?|m)?)', 'minutes'),
            (r'\bafter\s*loss\s*cooldown(?:\s*(?:to|=)\s*|\s+)([0-9.]+\s*(?:minutes?|mins?|m)?)', 'minutes'),
        ],
        'cycle_interval_minutes': [
            (r'\bcycle\s*(?:interval|cadence|delay)?\b(?:\s*(?:to|=)\s*|\s+)([0-9.]+\s*(?:minutes?|mins?|m)?)', 'minutes'),
            (r'\bupdate\s*every\b(?:\s*|\s+)([0-9.]+\s*(?:minutes?|mins?|m)?)', 'minutes'),
        ],
    }
    parsers = {
        'ratio': _parse_ratio_value,
        'money': _parse_money_value,
        'int': _parse_integer_value,
        'minutes': _parse_minutes_value,
    }
    for key, pattern_specs in patterns.items():
        for pattern, value_type in pattern_specs:
            match = re.search(pattern, lowered)
            if not match:
                continue
            parsed = parsers[value_type](match.group(1))
            if parsed is not None:
                overrides[key] = parsed
            break
    return overrides


def parse_control_message(message: str, *, prefixes: Sequence[str] = CONTROL_PREFIXES) -> ControlUpdate | None:
    text = _strip_optional_control_prefix(message, prefixes)
    if not text.strip():
        return None

    lowered = text.lower()
    if lowered in {'status', 'help', 'reset'}:
        return ControlUpdate(summary=lowered)

    # Pause / resume trading commands
    if _PAUSE_PATTERN.search(lowered):
        return ControlUpdate(trading_paused=True, summary='trading_paused=True')
    if _RESUME_PATTERN.search(lowered):
        return ControlUpdate(trading_paused=False, summary='trading_paused=False')

    update = ControlUpdate()

    profile = _extract_profile(text)
    if profile:
        update.execution_profile = profile

    label = _extract_label(text)
    if label:
        update.strategy_label = label

    skill_tags, replace_tags, clear_tags, remove_tags = _extract_skill_tags(text)
    if remove_tags:
        update.remove_skill_tags = skill_tags
    elif skill_tags or replace_tags or clear_tags:
        update.skill_tags = skill_tags
        update.replace_skill_tags = replace_tags or bool(skill_tags and not clear_tags)
        update.clear_skill_tags = clear_tags

    if re.search(r'\b(reset|clear|wipe)\s+(?:strategy|controls|overrides)\b', lowered):
        update.reset_strategy = True
        update.clear_live_overrides = True

    update.live_overrides.update(_extract_numeric_overrides(text))

    if update.reset_strategy and not any(
        [
            update.execution_profile,
            update.strategy_label,
            update.skill_tags,
            update.remove_skill_tags,
            update.live_overrides,
            update.clear_skill_tags,
        ]
    ):
        update.summary = 'reset strategy'
        return update

    if not any(
        [
            update.execution_profile,
            update.strategy_label,
            update.skill_tags,
            update.remove_skill_tags,
            update.live_overrides,
            update.clear_skill_tags,
            update.clear_live_overrides,
        ]
    ):
        return None

    summary_parts: list[str] = []
    if update.execution_profile:
        summary_parts.append(f'profile={update.execution_profile}')
    if update.strategy_label:
        summary_parts.append(f'strategy={update.strategy_label}')
    if update.skill_tags:
        summary_parts.append(f'skills={",".join(update.skill_tags)}')
    if update.remove_skill_tags:
        summary_parts.append(f'remove_skills={",".join(update.remove_skill_tags)}')
    for key, value in update.live_overrides.items():
        summary_parts.append(f'{key}={value}')
    if update.reset_strategy:
        summary_parts.append('strategy_reset')
    update.summary = '; '.join(summary_parts)
    return update


def apply_control_update(current_state: Mapping[str, Any], update: ControlUpdate, *, author_id: int | str | None = None, channel_id: int | str | None = None, command_text: str | None = None) -> dict[str, Any]:
    state = dict(DEFAULT_CONTROL_STATE)
    if isinstance(current_state, Mapping):
        state.update(current_state)

    if update.reset_strategy:
        state['execution_profile'] = 'balanced'
        state['live_overrides'] = {}
        state['strategy_label'] = None
        state['skill_tags'] = []

    if update.execution_profile:
        state['execution_profile'] = update.execution_profile

    if update.strategy_label is not None:
        state['strategy_label'] = update.strategy_label

    if update.clear_skill_tags:
        state['skill_tags'] = []
    if update.skill_tags:
        if update.replace_skill_tags:
            state['skill_tags'] = list(dict.fromkeys(update.skill_tags))
        else:
            existing = _coerce_skill_tags(state.get('skill_tags'))
            merged = list(dict.fromkeys(existing + update.skill_tags))
            state['skill_tags'] = merged
    if update.remove_skill_tags:
        existing = _coerce_skill_tags(state.get('skill_tags'))
        removals = {tag.lower() for tag in update.remove_skill_tags}
        state['skill_tags'] = [tag for tag in existing if tag.lower() not in removals]

    live_overrides = state.get('live_overrides')
    if not isinstance(live_overrides, dict):
        live_overrides = {}
    if update.clear_live_overrides:
        live_overrides = {}
    for key, value in update.live_overrides.items():
        if key in FLOAT_KEYS or key in INT_KEYS:
            live_overrides[key] = value
    state['live_overrides'] = live_overrides

    if update.trading_paused is not None:
        state['trading_paused'] = update.trading_paused

    if command_text is not None:
        state['last_command'] = command_text.strip()
    if author_id is not None:
        state['last_user_id'] = str(author_id)
    if channel_id is not None:
        state['last_channel_id'] = str(channel_id)
    state['updated_at'] = current_ts()
    return state


def summarize_control_state(state: Mapping[str, Any]) -> str:
    profile = state.get('execution_profile') or 'balanced'
    strategy_label = state.get('strategy_label')
    skill_tags = _coerce_skill_tags(state.get('skill_tags'))
    live_overrides = state.get('live_overrides') if isinstance(state.get('live_overrides'), dict) else {}
    trading_paused = state.get('trading_paused', False)
    parts = [f'profile={profile}']
    if trading_paused:
        parts.append('trading=PAUSED')
    if strategy_label:
        parts.append(f'strategy={strategy_label}')
    if skill_tags:
        parts.append(f'skills={",".join(skill_tags)}')
    if live_overrides:
        parts.append(
            'overrides='
            + ','.join(f'{key}={value}' for key, value in sorted(live_overrides.items()))
        )
    return '; '.join(parts)


def load_discord_env(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    values = env if env is not None else os.environ
    token = str(values.get('DISCORD_BOT_TOKEN') or '').strip()
    if not token:
        raise RuntimeError('DISCORD_BOT_TOKEN is required for Discord control mode')

    allowed_user_ids_raw = str(values.get('DISCORD_ALLOWED_USER_IDS') or '').strip()
    try:
        allowed_user_ids = {
            int(item.strip())
            for item in allowed_user_ids_raw.split(',')
            if item.strip()
        }
    except ValueError as exc:
        raise RuntimeError('DISCORD_ALLOWED_USER_IDS must be a comma-separated list of Discord user IDs') from exc
    if not allowed_user_ids:
        raise RuntimeError('DISCORD_ALLOWED_USER_IDS is required for Discord control mode')

    control_channel_id_raw = str(values.get('DISCORD_CONTROL_CHANNEL_ID') or '').strip()
    try:
        control_channel_id = int(control_channel_id_raw) if control_channel_id_raw else None
    except ValueError as exc:
        raise RuntimeError('DISCORD_CONTROL_CHANNEL_ID must be a Discord channel ID') from exc
    command_prefix = str(values.get('DISCORD_CONTROL_PREFIX') or 'simmer:').strip() or 'simmer:'
    if not command_prefix.endswith(':'):
        command_prefix += ':'
    return {
        'token': token,
        'allowed_user_ids': allowed_user_ids,
        'control_channel_id': control_channel_id,
        'command_prefix': command_prefix,
    }


async def run_discord_control_bot(
    *,
    state_path: Path,
    data_dir: Path | None = None,
    settings: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    try:
        import discord
    except ImportError as exc:  # pragma: no cover - exercised only in runtime mode
        raise RuntimeError('discord.py is required for Discord control mode: pip install discord.py') from exc

    settings = settings or load_discord_env(env)
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = False

    client = discord.Client(intents=intents)
    tree = discord.app_commands.CommandTree(client)
    prefixes = tuple(
        dict.fromkeys(
            [
                str(settings.get('command_prefix') or '').strip().lower(),
                *CONTROL_PREFIXES,
            ]
        )
    )

    def _is_allowed(message) -> bool:
        if message.author.bot:
            return False
        if int(message.author.id) not in settings['allowed_user_ids']:
            return False
        if settings['control_channel_id'] is not None and int(message.channel.id) != settings['control_channel_id']:
            return False
        return True

    def _is_allowed_interaction(interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) not in settings['allowed_user_ids']:
            return False
        if settings['control_channel_id'] is not None and int(interaction.channel_id) != settings['control_channel_id']:
            return False
        return True

    # ------------------------------------------------------------------
    # Slash command group: /simmer <subcommand>
    # ------------------------------------------------------------------
    simmer_group = discord.app_commands.Group(name='simmer', description='Simmer trading bot controls')

    @simmer_group.command(name='status', description='Show current bot control state')
    async def slash_status(interaction: discord.Interaction) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        current_state = load_control_state(state_path)
        await interaction.response.send_message(
            f'Current control state: {summarize_control_state(current_state)}', ephemeral=False
        )

    @simmer_group.command(name='skills', description='List all installed ClawHub skills and signal registry')
    async def slash_skills(interaction: discord.Interaction) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        if data_dir is None:
            await interaction.response.send_message('⚠️ data_dir not configured.', ephemeral=True)
            return
        await interaction.response.defer()
        try:
            from btc_skill_stack import execute_skill_command, parse_skill_command
            result = execute_skill_command(parse_skill_command('list skills'), data_dir)
        except Exception as exc:
            result = f'⚠️ {exc}'
        for chunk in _chunk_message(result, 1900):
            await interaction.followup.send(chunk)

    @simmer_group.command(name='discover', description='Discover ClawHub skills (optionally search by keyword)')
    @discord.app_commands.describe(query='Search keywords (leave blank to browse top skills)')
    async def slash_discover(interaction: discord.Interaction, query: str = '') -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        if data_dir is None:
            await interaction.response.send_message('⚠️ data_dir not configured.', ephemeral=True)
            return
        await interaction.response.defer()
        try:
            from btc_clawhub_skills import execute_clawhub_command
            cmd = {'op': 'discover', 'query': query}
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: execute_clawhub_command(cmd, data_dir=data_dir)
            )
        except Exception as exc:
            result = f'⚠️ {exc}'
        for chunk in _chunk_message(result, 1900):
            await interaction.followup.send(chunk)

    @simmer_group.command(name='install', description='Install a ClawHub skill by slug')
    @discord.app_commands.describe(slug='Skill slug (e.g. polymarket-copytrading)')
    async def slash_install(interaction: discord.Interaction, slug: str) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        if data_dir is None:
            await interaction.response.send_message('⚠️ data_dir not configured.', ephemeral=True)
            return
        await interaction.response.defer()
        try:
            from btc_clawhub_skills import execute_clawhub_command
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: execute_clawhub_command({'op': 'install', 'slug': slug}, data_dir=data_dir)
            )
        except Exception as exc:
            result = f'⚠️ {exc}'
        for chunk in _chunk_message(result, 1900):
            await interaction.followup.send(chunk)

    @simmer_group.command(name='remove', description='Uninstall a ClawHub skill by slug')
    @discord.app_commands.describe(slug='Skill slug to remove')
    async def slash_remove(interaction: discord.Interaction, slug: str) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        if data_dir is None:
            await interaction.response.send_message('⚠️ data_dir not configured.', ephemeral=True)
            return
        await interaction.response.defer()
        try:
            from btc_clawhub_skills import execute_clawhub_command
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: execute_clawhub_command({'op': 'remove', 'slug': slug}, data_dir=data_dir)
            )
        except Exception as exc:
            result = f'⚠️ {exc}'
        for chunk in _chunk_message(result, 1900):
            await interaction.followup.send(chunk)

    @simmer_group.command(name='inspect', description='Inspect a ClawHub skill')
    @discord.app_commands.describe(slug='Skill slug to inspect')
    async def slash_inspect(interaction: discord.Interaction, slug: str) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        if data_dir is None:
            await interaction.response.send_message('⚠️ data_dir not configured.', ephemeral=True)
            return
        await interaction.response.defer()
        try:
            from btc_clawhub_skills import execute_clawhub_command
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: execute_clawhub_command({'op': 'inspect', 'slug': slug}, data_dir=data_dir)
            )
        except Exception as exc:
            result = f'⚠️ {exc}'
        for chunk in _chunk_message(result, 1900):
            await interaction.followup.send(chunk)

    @simmer_group.command(name='performance', description='Show P&L by skill from Simmer briefing')
    async def slash_performance(interaction: discord.Interaction) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        if data_dir is None:
            await interaction.response.send_message('⚠️ data_dir not configured.', ephemeral=True)
            return
        await interaction.response.defer()
        try:
            from btc_clawhub_skills import execute_clawhub_command
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: execute_clawhub_command({'op': 'performance'}, data_dir=data_dir)
            )
        except Exception as exc:
            result = f'⚠️ {exc}'
        for chunk in _chunk_message(result, 1900):
            await interaction.followup.send(chunk)

    @simmer_group.command(name='gameplan', description='Get analyst gameplan for next session')
    async def slash_gameplan(interaction: discord.Interaction) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        if data_dir is None:
            await interaction.response.send_message('⚠️ data_dir not configured.', ephemeral=True)
            return
        await interaction.response.defer()
        try:
            from btc_analyst import execute_analyst_command, parse_analyst_command
            result = execute_analyst_command(parse_analyst_command('gameplan'), data_dir, config=None)
        except Exception as exc:
            result = f'⚠️ {exc}'
        for chunk in _chunk_message(result, 1900):
            await interaction.followup.send(chunk)

    @simmer_group.command(name='review', description='Review current trading session')
    async def slash_review(interaction: discord.Interaction) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        if data_dir is None:
            await interaction.response.send_message('⚠️ data_dir not configured.', ephemeral=True)
            return
        await interaction.response.defer()
        try:
            from btc_analyst import execute_analyst_command, parse_analyst_command
            result = execute_analyst_command(parse_analyst_command('review session'), data_dir, config=None)
        except Exception as exc:
            result = f'⚠️ {exc}'
        for chunk in _chunk_message(result, 1900):
            await interaction.followup.send(chunk)

    @simmer_group.command(name='edge', description='Set minimum edge threshold')
    @discord.app_commands.describe(value='Edge value (e.g. 0.08)')
    async def slash_edge(interaction: discord.Interaction, value: str) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        command = parse_control_message(f'set min edge to {value}', prefixes=())
        if command is None:
            await interaction.response.send_message(f'⚠️ Could not parse edge value: {value!r}', ephemeral=True)
            return
        current_state = load_control_state(state_path)
        updated_state = apply_control_update(
            current_state, command,
            author_id=getattr(interaction.user, 'id', None),
            channel_id=interaction.channel_id,
            command_text=f'set min edge to {value}',
        )
        write_control_state(state_path, updated_state)
        await interaction.response.send_message(
            f'Applied: {summarize_control_state(updated_state)}'
        )

    @simmer_group.command(name='aggressive', description='Switch to aggressive trading profile')
    async def slash_aggressive(interaction: discord.Interaction) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        command = parse_control_message('be more aggressive', prefixes=())
        current_state = load_control_state(state_path)
        updated_state = apply_control_update(
            current_state, command,
            author_id=getattr(interaction.user, 'id', None),
            channel_id=interaction.channel_id,
            command_text='be more aggressive',
        )
        write_control_state(state_path, updated_state)
        await interaction.response.send_message(f'Applied: {summarize_control_state(updated_state)}')

    @simmer_group.command(name='cautious', description='Switch to cautious trading profile')
    async def slash_cautious(interaction: discord.Interaction) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        command = parse_control_message('be more cautious', prefixes=())
        current_state = load_control_state(state_path)
        updated_state = apply_control_update(
            current_state, command,
            author_id=getattr(interaction.user, 'id', None),
            channel_id=interaction.channel_id,
            command_text='be more cautious',
        )
        write_control_state(state_path, updated_state)
        await interaction.response.send_message(f'Applied: {summarize_control_state(updated_state)}')

    @simmer_group.command(name='pause', description='Pause all trading immediately')
    async def slash_pause(interaction: discord.Interaction) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        current_state = load_control_state(state_path)
        updated_state = apply_control_update(
            current_state,
            ControlUpdate(trading_paused=True, summary='trading_paused=True'),
            author_id=getattr(interaction.user, 'id', None),
            channel_id=interaction.channel_id,
            command_text='pause trading',
        )
        write_control_state(state_path, updated_state)
        await interaction.response.send_message('⏸️ Trading paused. Use `/simmer resume` to restart.')

    @simmer_group.command(name='resume', description='Resume trading after a pause')
    async def slash_resume(interaction: discord.Interaction) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        current_state = load_control_state(state_path)
        updated_state = apply_control_update(
            current_state,
            ControlUpdate(trading_paused=False, summary='trading_paused=False'),
            author_id=getattr(interaction.user, 'id', None),
            channel_id=interaction.channel_id,
            command_text='resume trading',
        )
        write_control_state(state_path, updated_state)
        await interaction.response.send_message('▶️ Trading resumed.')

    @simmer_group.command(name='ask', description='Send a free-form NL command to the bot')
    @discord.app_commands.describe(text='What you want the bot to do')
    async def slash_ask(interaction: discord.Interaction, text: str) -> None:
        if not _is_allowed_interaction(interaction):
            await interaction.response.send_message('❌ Not authorized.', ephemeral=True)
            return
        await interaction.response.defer()
        # Route through existing parsers in order
        if data_dir is not None:
            try:
                from btc_skill_stack import execute_skill_command, parse_skill_command
                skill_cmd = parse_skill_command(text)
                if skill_cmd is not None:
                    result = execute_skill_command(skill_cmd, data_dir)
                    await interaction.followup.send(result)
                    return
            except Exception as exc:
                await interaction.followup.send(f'⚠️ {exc}')
                return
            try:
                from btc_clawhub_skills import execute_clawhub_command, parse_clawhub_command
                clawhub_cmd = parse_clawhub_command(text)
                if clawhub_cmd is not None:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: execute_clawhub_command(clawhub_cmd, data_dir=data_dir)
                    )
                    for chunk in _chunk_message(result, 1900):
                        await interaction.followup.send(chunk)
                    return
            except Exception as exc:
                await interaction.followup.send(f'⚠️ {exc}')
                return
            try:
                from btc_analyst import execute_analyst_command, parse_analyst_command
                analyst_cmd = parse_analyst_command(text)
                if analyst_cmd is not None:
                    result = execute_analyst_command(analyst_cmd, data_dir, config=None)
                    for chunk in _chunk_message(result, 1900):
                        await interaction.followup.send(chunk)
                    return
            except Exception as exc:
                await interaction.followup.send(f'⚠️ {exc}')
                return
        command = parse_control_message(text, prefixes=())
        if command is None:
            await interaction.followup.send(
                'I did not understand that. Try `/simmer aggressive`, `/simmer edge 0.08`, '
                '`/simmer pause`, `/simmer resume`, `/simmer discover`, or `/simmer ask <free text>`.'
            )
            return
        current_state = load_control_state(state_path)
        updated_state = apply_control_update(
            current_state, command,
            author_id=getattr(interaction.user, 'id', None),
            channel_id=interaction.channel_id,
            command_text=text,
        )
        write_control_state(state_path, updated_state)
        await interaction.followup.send(f'Applied: {summarize_control_state(updated_state)}')

    tree.add_command(simmer_group)

    @client.event
    async def on_ready() -> None:  # pragma: no cover - runtime feedback only
        await tree.sync()
        print(
            json.dumps(
                {
                    'discord_control': 'ready',
                    'user': str(getattr(client.user, 'name', None)),
                    'state_path': str(state_path),
                    'slash_commands_synced': True,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    @client.event
    async def on_message(message) -> None:  # pragma: no cover - runtime integration
        if not _is_allowed(message):
            return
        content = str(message.content or '').strip()
        if not content:
            return
        had_prefix = any(content.lower().startswith(prefix) for prefix in prefixes if prefix)
        if not had_prefix:
            return

        # Strip prefix so sub-parsers see clean text
        stripped = _strip_optional_control_prefix(content, prefixes)

        # --- Skill stack commands (add/enable/disable/remove/list/show skill) ---
        if data_dir is not None:
            try:
                from btc_skill_stack import execute_skill_command, parse_skill_command
                skill_cmd = parse_skill_command(stripped)
                if skill_cmd is not None:
                    await message.reply(
                        execute_skill_command(skill_cmd, data_dir),
                        mention_author=False,
                    )
                    return
            except Exception as exc:
                await message.reply(f'⚠️ Skill command error: {exc}', mention_author=False)
                return

        # --- ClawHub skill management (install/remove/discover/inspect/performance) ---
        if data_dir is not None:
            try:
                from btc_clawhub_skills import execute_clawhub_command, parse_clawhub_command
                clawhub_cmd = parse_clawhub_command(stripped)
                if clawhub_cmd is not None:
                    await message.reply('⏳ Working...', mention_author=False)
                    result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: execute_clawhub_command(clawhub_cmd, data_dir=data_dir)
                    )
                    for chunk in _chunk_message(result, 1900):
                        await message.channel.send(chunk)
                    return
            except Exception as exc:
                await message.reply(f'⚠️ ClawHub error: {exc}', mention_author=False)
                return

        # --- Analyst commands (review session, brainstorm, critique skill) ---
        if data_dir is not None:
            try:
                from btc_analyst import execute_analyst_command, parse_analyst_command
                analyst_cmd = parse_analyst_command(stripped)
                if analyst_cmd is not None:
                    await message.reply('⏳ Thinking...', mention_author=False)
                    current_config = None
                    try:
                        import json as _json
                        _cfg_path = state_path.parent / '..' / 'config' / 'defaults.json'
                        if _cfg_path.exists():
                            current_config = _json.loads(_cfg_path.read_text())
                    except Exception:
                        pass
                    reply_text = execute_analyst_command(analyst_cmd, data_dir, config=current_config)
                    # Discord has a 2000-char limit per message; chunk if needed
                    for chunk in _chunk_message(reply_text, 1900):
                        await message.channel.send(chunk)
                    return
            except Exception as exc:
                await message.reply(f'⚠️ Analyst error: {exc}', mention_author=False)
                return

        # --- NL control commands (profile, edge, strategy, skill tags, etc.) ---
        command = parse_control_message(content, prefixes=prefixes)
        if command is None:
            await message.reply(
                'I did not understand that. Try `be more aggressive`, `set min edge to 0.08`, '
                '`pause trading`, `resume trading`, '
                '`list skills`, `discover polymarket-fast-loop`, `install skill <slug>`, '
                '`review session`, or `gameplan`.',
                mention_author=False,
            )
            return
        if command.summary in {'help', 'status'} and not any(
            [
                command.execution_profile,
                command.strategy_label,
                command.skill_tags,
                command.remove_skill_tags,
                command.live_overrides,
                command.clear_skill_tags,
                command.clear_live_overrides,
                command.reset_strategy,
            ]
        ):
            current_state = load_control_state(state_path)
            if command.summary == 'status':
                reply_text = f'Current control state: {summarize_control_state(current_state)}'
            else:
                reply_text = (
                    'Commands: `be more aggressive`, `set min edge to 0.08`, '
                    '`pause trading`, `resume trading`, '
                    '`add skill <name>: <description>`, `enable skill <name>`, '
                    '`list skills`, `review session`, `gameplan`, '
                    '`critique: <skill content>`, `reset strategy`.'
                )
            await message.reply(reply_text, mention_author=False)
            return
        current_state = load_control_state(state_path)
        updated_state = apply_control_update(
            current_state,
            command,
            author_id=getattr(message.author, 'id', None),
            channel_id=getattr(message.channel, 'id', None),
            command_text=content,
        )
        write_control_state(state_path, updated_state)
        await message.reply(
            f'Applied control update: {summarize_control_state(updated_state)}',
            mention_author=False,
        )

    await client.start(settings['token'])


def _chunk_message(text: str, limit: int = 1900) -> list[str]:
    """Split *text* into chunks of at most *limit* characters."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def start_discord_control_thread(
    *,
    state_path: Path,
    data_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
):
    import threading

    settings = load_discord_env(env)
    try:
        import discord  # noqa: F401
    except ImportError as exc:  # pragma: no cover - runtime validation only
        raise RuntimeError('discord.py is required for Discord control mode: pip install discord.py') from exc

    thread = threading.Thread(
        target=lambda: asyncio.run(
            run_discord_control_bot(
                state_path=state_path,
                data_dir=data_dir,
                settings=settings,
                env=env,
            )
        ),
        name='discord-control',
        daemon=True,
    )
    thread.start()
    return thread
