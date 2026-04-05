from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))

import btc_discord_alert  # noqa: E402


def test_load_discord_webhook_url_requires_env():
    try:
        btc_discord_alert.load_discord_webhook_url({})
    except btc_discord_alert.DiscordAlertError as exc:
        assert 'DISCORD_WEBHOOK_URL is required' in str(exc)
    else:
        raise AssertionError('expected DiscordAlertError')


def test_send_discord_alert_posts_payload(monkeypatch):
    captured = {}

    class DummyResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"id":"123","content":"BTC sprint bot Discord test alert"}'

        def getcode(self):
            return self.status

    def fake_urlopen(request, timeout=0):
        captured['url'] = request.full_url
        captured['body'] = request.data.decode('utf-8')
        captured['timeout'] = timeout
        return DummyResponse()

    monkeypatch.setattr(btc_discord_alert, 'urlopen', fake_urlopen)

    result = btc_discord_alert.send_discord_alert(
        'BTC sprint bot Discord test alert',
        env={'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/test/test'},
        timeout=3.0,
    )

    assert captured['url'].endswith('?wait=true')
    assert json.loads(captured['body']) == {
        'content': 'BTC sprint bot Discord test alert',
        'allowed_mentions': {'parse': []},
    }
    assert captured['timeout'] == 3.0
    assert result['ok'] is True
    assert result['status_code'] == 204
    assert result['response']['id'] == '123'
