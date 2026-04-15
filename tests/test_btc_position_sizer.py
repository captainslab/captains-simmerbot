from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / 'skills' / 'btc-sprint-stack'
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from engine.position_sizer import PositionSizer, PositionSizerConfig, SizingBalanceSnapshot
from execution.order_state_machine import OrderStateMachine
from execution.trade_executor import DecisionRecord, TradeExecutor


def _decision(**overrides) -> DecisionRecord:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        'decision_id': 'round-1',
        'round_id': 'round-1',
        'market_id': 'btc-5m',
        'action': 'buy_yes',
        'final_action': 'buy_yes',
        'amount': 4.0,
        'edge': 1.0,
        'gate_result': {'allowed': True, 'reasons': []},
        'feature_summary': {
            'health_state': 'ok',
            'available_balance_usdc': 20.0,
            'observed_at': now,
        },
    }
    payload.update(overrides)
    return DecisionRecord(**payload)


def _balance(*, amount: float | None = 20.0, age_seconds: float = 0.0) -> SizingBalanceSnapshot:
    return SizingBalanceSnapshot(
        available_usdc=amount,
        fetched_at=(datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
    )


class RecordingBroker:
    def __init__(self, *, balance: SizingBalanceSnapshot | None = None) -> None:
        self.submit_calls = 0
        self.balance = balance or _balance()
        self.state_machine = OrderStateMachine()

    def fetch_balance(self):
        return self.balance

    def submit_order(
        self,
        *,
        idempotency_key: str,
        market_id: str,
        side: str,
        amount: float,
        reasoning=None,
        signal_data=None,
        readiness_status=None,
        auth_status=None,
    ):
        del reasoning, signal_data, readiness_status, auth_status
        self.submit_calls += 1
        intent = self.state_machine.create_intent(
            idempotency_key=idempotency_key,
            market_id=market_id,
            side=side,
            amount=amount,
        )
        self.state_machine.transition(intent, 'submitted', provider_order_id=f'order-{self.submit_calls}')
        self.state_machine.transition(intent, 'acknowledged', provider_order_id=f'order-{self.submit_calls}')
        return intent


def test_buy_yes_produces_bounded_nonzero_size():
    sizer = PositionSizer()

    sizing = sizer.size(decision=_decision(action='buy_yes'), balance_snapshot=_balance())

    assert sizing.allowed is True
    assert sizing.size == 4.0
    assert sizing.notional == 4.0


def test_buy_no_produces_bounded_nonzero_size():
    sizer = PositionSizer()

    sizing = sizer.size(decision=_decision(action='buy_no', final_action='buy_no'), balance_snapshot=_balance())

    assert sizing.allowed is True
    assert sizing.size == 4.0
    assert sizing.notional == 4.0


def test_no_trade_produces_zero_size_and_no_submit():
    broker = RecordingBroker()
    executor = TradeExecutor(broker=broker)

    result = executor.execute(_decision(action='no_trade', final_action='no_trade'))

    assert result.sizing is not None
    assert result.sizing.size == 0.0
    assert broker.submit_calls == 0


def test_sub_minimum_post_scaling_size_is_rejected_instead_of_silently_submitted():
    broker = RecordingBroker()
    executor = TradeExecutor(
        broker=broker,
        position_sizer_config=PositionSizerConfig(min_size=1.5, max_size=2.0, max_balance_fraction=1.0, min_edge=0.5),
    )

    result = executor.execute(_decision(edge=0.6, amount=2.0))

    assert result.outcome == 'no_trade'
    assert result.sizing is not None
    assert result.sizing.allowed is False
    assert result.sizing.proposed_size == 1.2
    assert any(reason.startswith('below_minimum_size:') for reason in result.sizing.reasons)
    assert broker.submit_calls == 0


def test_max_balance_fraction_cap_is_enforced():
    sizer = PositionSizer(config=PositionSizerConfig(max_size=10.0, max_balance_fraction=0.25))

    sizing = sizer.size(decision=_decision(amount=8.0), balance_snapshot=_balance(amount=10.0))

    assert sizing.allowed is True
    assert sizing.size == 2.5


def test_stale_missing_balance_blocks_sizing():
    sizer = PositionSizer()

    stale = sizer.size(decision=_decision(), balance_snapshot=_balance(age_seconds=120.0))
    missing = sizer.size(decision=_decision(), balance_snapshot=_balance(amount=None))

    assert stale.allowed is False
    assert 'stale_balance:120.0s' in stale.reasons
    assert missing.allowed is False
    assert 'missing_balance' in missing.reasons


def test_duplicate_execution_still_does_not_duplicate_submit():
    broker = RecordingBroker()
    executor = TradeExecutor(broker=broker)
    decision = _decision(decision_id='dup', round_id='dup')

    first = executor.execute(decision)
    second = executor.execute(decision)

    assert first is second
    assert broker.submit_calls == 1
    assert second.events[-1].event_type == 'duplicate_execution_ignored'
