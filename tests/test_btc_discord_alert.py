from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))

import btc_discord_alert  # noqa: E402


def test_send_discord_alert_includes_allowed_mentions(monkeypatch):
    captured = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(request, timeout=15.0):
        captured['url'] = request.full_url
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _Response()

    monkeypatch.setattr(btc_discord_alert, 'urlopen', fake_urlopen)
    monkeypatch.setattr(
        btc_discord_alert,
        'load_discord_webhook_url',
        lambda env=None: 'https://discord.com/api/webhooks/123/abc',
    )

    result = btc_discord_alert.send_discord_alert(
        'hello',
        mention_user_ids=['123', 456],
    )

    assert result['ok'] is True
    assert captured['url'].endswith('wait=true')
    assert captured['body']['content'] == 'hello'
    assert captured['body']['allowed_mentions']['parse'] == []
    assert captured['body']['allowed_mentions']['users'] == ['123', '456']
