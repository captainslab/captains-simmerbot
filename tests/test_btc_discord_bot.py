from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))

import btc_discord_bot  # noqa: E402


def test_parse_incoming_message_routes_question_prefix_and_commands():
    question = btc_discord_bot._parse_incoming_message('? what looks good', is_mentioned=False)
    assert question['kind'] == 'llm'
    assert question['text'] == 'what looks good'

    command = btc_discord_bot._parse_incoming_message('!status', is_mentioned=False)
    assert command['kind'] == 'command'
    assert command['command'] == '!status'
    assert command['parts'] == ['!status']

    mention = btc_discord_bot._parse_incoming_message(
        '<@123> give me a briefing',
        is_mentioned=True,
        bot_user_id=123,
    )
    assert mention['kind'] == 'llm'
    assert mention['text'] == 'give me a briefing'


def test_split_bot_actions_keeps_only_supported_actions():
    clean, actions = btc_discord_bot._split_bot_actions(
        'Status update\n'
        'BOT_ACTION:cycle:\n'
        'BOT_ACTION:experimental:ignored\n'
        'BOT_ACTION:alert:btc_price:lt:80000\n'
    )

    assert clean == 'Status update'
    assert actions == ['BOT_ACTION:cycle:', 'BOT_ACTION:alert:btc_price:lt:80000']
