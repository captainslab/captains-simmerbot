"""Tests for btc_skill_stack — registry CRUD, signal computation, blending."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))

from btc_skill_stack import (
    SkillEntry,
    _detect_signal_type,
    _extract_params,
    _sanitize_id,
    blend_signals,
    build_blended_signal,
    create_skill,
    execute_skill_command,
    get_active_skills,
    load_registry,
    parse_skill_command,
    remove_skill,
    save_registry,
    set_skill_enabled,
    _compute_rsi,
    _compute_ema,
)
from btc_sprint_signal import SignalDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(action="yes", edge=0.1, confidence=0.7, source="test") -> SignalDecision:
    return SignalDecision(action=action, edge=edge, confidence=confidence,
                         signal_source=source, reasoning="test", metrics={})


# ---------------------------------------------------------------------------
# _sanitize_id
# ---------------------------------------------------------------------------

def test_sanitize_id_basic():
    assert _sanitize_id("RSI Reversal") == "rsi-reversal"


def test_sanitize_id_special_chars():
    assert _sanitize_id("VWAP/Mean-Reversion!!") == "vwap-mean-reversion"


def test_sanitize_id_truncates():
    assert len(_sanitize_id("a" * 60)) <= 40


# ---------------------------------------------------------------------------
# _detect_signal_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("buy when RSI drops below 30", "rsi"),
    ("use RSI(14) oversold strategy", "rsi"),
    ("EMA crossover 5 vs 13", "ma_cross"),
    ("moving average cross strategy", "ma_cross"),
    ("donchian channel breakout", "breakout"),
    ("breakout above 20-period high", "breakout"),
    ("VWAP mean reversion", "vwap"),
    ("short momentum on 3/8 windows", "momentum"),
    ("some random strategy", "momentum"),  # default
])
def test_detect_signal_type(desc, expected):
    assert _detect_signal_type(desc) == expected


# ---------------------------------------------------------------------------
# _extract_params
# ---------------------------------------------------------------------------

def test_extract_rsi_params():
    params = _extract_params("buy when RSI(21) < 25, sell when > 75", "rsi")
    assert params["period"] == 21
    assert params["oversold"] == 25
    assert params["overbought"] == 75


def test_extract_ma_cross_params():
    params = _extract_params("EMA 3 vs 8 crossover", "ma_cross")
    assert params["short_ema"] == 3
    assert params["long_ema"] == 8


def test_extract_breakout_params():
    params = _extract_params("30 period channel breakout", "breakout")
    assert params["period"] == 30


def test_extract_vwap_params():
    params = _extract_params("0.5% deviation from VWAP", "vwap")
    assert abs(params["threshold"] - 0.005) < 1e-9


def test_extract_momentum_params():
    params = _extract_params("momentum 5 and 10 window", "momentum")
    assert params["short_window"] == 5
    assert params["long_window"] == 10


def test_extract_uses_defaults_when_no_match():
    params = _extract_params("RSI strategy", "rsi")
    assert params["period"] == 14
    assert params["oversold"] == 30
    assert params["overbought"] == 70


# ---------------------------------------------------------------------------
# Registry CRUD
# ---------------------------------------------------------------------------

def test_load_registry_missing(tmp_path):
    assert load_registry(tmp_path) == []


def test_save_and_load_registry(tmp_path):
    entry = SkillEntry(id="test-skill", name="Test", description="desc",
                       signal_type="rsi", params={"period": 14}, enabled=False)
    save_registry(tmp_path, [entry])
    loaded = load_registry(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].id == "test-skill"
    assert loaded[0].enabled is False


def test_save_registry_atomic(tmp_path):
    """registry.json should be a valid JSON file after save."""
    entry = SkillEntry(id="x", name="X", description="d", signal_type="momentum")
    save_registry(tmp_path, [entry])
    path = tmp_path / "skill_stack" / "registry.json"
    data = json.loads(path.read_text())
    assert "skills" in data
    assert "updated_at" in data


def test_create_skill_disabled_by_default(tmp_path):
    entry = create_skill(tmp_path, name="RSI Rev", description="buy when RSI < 30")
    assert entry.enabled is False


def test_create_skill_writes_skill_md(tmp_path):
    create_skill(tmp_path, name="test skill", description="momentum strategy")
    skill_md = tmp_path / "skill_stack" / "test-skill" / "SKILL.md"
    assert skill_md.exists()
    content = skill_md.read_text()
    assert "test-skill" in content
    assert "Disabled by default" in content


def test_create_skill_duplicate_raises(tmp_path):
    create_skill(tmp_path, name="my skill", description="test")
    with pytest.raises(ValueError, match="already exists"):
        create_skill(tmp_path, name="my skill", description="different")


def test_create_skill_detects_signal_type(tmp_path):
    entry = create_skill(tmp_path, name="rsi test", description="buy when RSI < 30")
    assert entry.signal_type == "rsi"


def test_remove_skill(tmp_path):
    create_skill(tmp_path, name="to remove", description="test")
    removed = remove_skill(tmp_path, "to remove")
    assert removed is not None
    assert removed.id == "to-remove"
    assert load_registry(tmp_path) == []


def test_remove_skill_not_found(tmp_path):
    assert remove_skill(tmp_path, "nonexistent") is None


def test_set_skill_enabled(tmp_path):
    create_skill(tmp_path, name="my skill", description="test")
    entry = set_skill_enabled(tmp_path, "my skill", enabled=True)
    assert entry.enabled is True
    loaded = load_registry(tmp_path)
    assert loaded[0].enabled is True


def test_set_skill_enabled_not_found(tmp_path):
    assert set_skill_enabled(tmp_path, "ghost", enabled=True) is None


def test_get_active_skills_excludes_disabled(tmp_path):
    create_skill(tmp_path, name="skill a", description="rsi strategy")
    create_skill(tmp_path, name="skill b", description="momentum strategy")
    set_skill_enabled(tmp_path, "skill-a", enabled=True)
    active = get_active_skills(tmp_path)
    assert len(active) == 1
    assert active[0].id == "skill-a"


# ---------------------------------------------------------------------------
# blend_signals
# ---------------------------------------------------------------------------

def test_blend_single_signal():
    sig = _make_signal("yes", 0.1)
    result = blend_signals([(sig, 1.0)])
    assert result is sig


def test_blend_unanimous_yes():
    sigs = [(_make_signal("yes", 0.1, 0.7), 1.0), (_make_signal("yes", 0.12, 0.75), 1.0)]
    result = blend_signals(sigs, min_edge=0.05)
    assert result.action == "yes"
    assert result.edge > 0


def test_blend_unanimous_no():
    sigs = [(_make_signal("no", 0.1, 0.7), 1.0), (_make_signal("no", 0.08, 0.65), 1.0)]
    result = blend_signals(sigs, min_edge=0.05)
    assert result.action == "no"


def test_blend_opposing_signals_hold():
    """yes and no of equal weight and equal edge → net=0 → hold."""
    sigs = [(_make_signal("yes", 0.1, 0.7), 1.0), (_make_signal("no", 0.1, 0.7), 1.0)]
    result = blend_signals(sigs, min_edge=0.0)
    assert result.action == "hold"


def test_blend_hold_pulls_toward_hold():
    """If one signal is hold (edge=0), its vote is zero; direction from others wins."""
    sigs = [
        (_make_signal("yes", 0.1, 0.7), 1.0),
        (_make_signal("hold", 0.0, 0.5), 1.0),
    ]
    result = blend_signals(sigs, min_edge=0.02)
    # net = (1*1*0.1 + 0*1*0) / 2 = 0.05 > 0 → yes
    assert result.action == "yes"


def test_blend_below_min_edge_is_hold():
    sigs = [(_make_signal("yes", 0.02, 0.6), 1.0), (_make_signal("yes", 0.03, 0.6), 1.0)]
    result = blend_signals(sigs, min_edge=0.07)
    assert result.action == "hold"
    assert result.edge == 0.0


def test_blend_constituents_in_metrics():
    sigs = [(_make_signal("yes", 0.1), 1.0), (_make_signal("no", 0.08), 0.5)]
    result = blend_signals(sigs)
    assert "constituents" in result.metrics
    assert len(result.metrics["constituents"]) == 2


def test_blend_empty_raises():
    with pytest.raises(ValueError):
        blend_signals([])


def test_blend_weighted():
    """Higher weight on yes signal should produce yes even if count is equal."""
    sigs = [
        (_make_signal("yes", 0.1, 0.7), 3.0),
        (_make_signal("no", 0.1, 0.7), 1.0),
    ]
    # net = (1*3*0.1 + (-1)*1*0.1) / 4 = (0.3-0.1)/4 = 0.05 > 0
    result = blend_signals(sigs, min_edge=0.04)
    assert result.action == "yes"


# ---------------------------------------------------------------------------
# _compute_rsi
# ---------------------------------------------------------------------------

def test_compute_rsi_insufficient_data():
    assert _compute_rsi([1.0, 2.0], period=14) == 50.0


def test_compute_rsi_all_gains_returns_100():
    closes = [float(i) for i in range(20)]  # strictly increasing
    rsi = _compute_rsi(closes, period=14)
    assert rsi == 100.0


def test_compute_rsi_all_losses_returns_0():
    closes = [float(20 - i) for i in range(20)]  # strictly decreasing
    rsi = _compute_rsi(closes, period=14)
    assert rsi < 5.0  # very low RSI


def test_compute_rsi_midrange():
    # Alternating gains and losses → RSI near 50
    closes = [100.0 + (1 if i % 2 == 0 else -1) for i in range(30)]
    rsi = _compute_rsi(closes, period=14)
    assert 40 < rsi < 60


# ---------------------------------------------------------------------------
# _compute_ema
# ---------------------------------------------------------------------------

def test_compute_ema_empty():
    assert _compute_ema([], 5) == 0.0


def test_compute_ema_single():
    assert _compute_ema([42.0], 5) == 42.0


def test_compute_ema_converges():
    # EMA of a constant series should equal the constant
    closes = [50.0] * 20
    assert abs(_compute_ema(closes, 5) - 50.0) < 1e-9


# ---------------------------------------------------------------------------
# parse_skill_command
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_op", [
    ("add skill rsi-reversal: buy when RSI < 30", "add"),
    ("create skill momentum-v2: use 5/10 windows", "add"),
    ("new skill breakout: donchian 20 period", "add"),
    ("remove skill rsi-reversal", "remove"),
    ("delete skill old-skill", "remove"),
    ("enable skill rsi-reversal", "enable"),
    ("disable skill my-skill", "disable"),
    ("list skills", "list"),
    ("show skills", "list"),
    ("show skill rsi-reversal", "show"),
])
def test_parse_skill_command_ops(text, expected_op):
    cmd = parse_skill_command(text)
    assert cmd is not None
    assert cmd["op"] == expected_op


def test_parse_skill_command_add_extracts_fields():
    cmd = parse_skill_command("add skill rsi rev: buy when RSI drops below 30 oversold")
    assert cmd["op"] == "add"
    assert cmd["name"] == "rsi rev"
    assert "RSI" in cmd["description"] or "rsi" in cmd["description"].lower()


def test_parse_skill_command_no_match():
    assert parse_skill_command("set min edge to 0.08") is None
    assert parse_skill_command("be more aggressive") is None
    assert parse_skill_command("") is None


# ---------------------------------------------------------------------------
# execute_skill_command
# ---------------------------------------------------------------------------

def test_execute_skill_command_add(tmp_path):
    cmd = {"op": "add", "name": "rsi test", "description": "buy when RSI < 30"}
    reply = execute_skill_command(cmd, tmp_path)
    assert "rsi-test" in reply
    assert "Disabled by default" in reply or "disabled" in reply.lower()


def test_execute_skill_command_add_duplicate(tmp_path):
    create_skill(tmp_path, name="dup", description="test")
    cmd = {"op": "add", "name": "dup", "description": "different"}
    reply = execute_skill_command(cmd, tmp_path)
    assert "⚠️" in reply


def test_execute_skill_command_enable(tmp_path):
    create_skill(tmp_path, name="my skill", description="test")
    reply = execute_skill_command({"op": "enable", "name": "my-skill"}, tmp_path)
    assert "enabled" in reply.lower()


def test_execute_skill_command_enable_not_found(tmp_path):
    reply = execute_skill_command({"op": "enable", "name": "ghost"}, tmp_path)
    assert "⚠️" in reply


def test_execute_skill_command_list_empty(tmp_path):
    reply = execute_skill_command({"op": "list"}, tmp_path)
    assert "No custom skills" in reply


def test_execute_skill_command_list_with_skills(tmp_path):
    create_skill(tmp_path, name="skill one", description="rsi strategy")
    reply = execute_skill_command({"op": "list"}, tmp_path)
    assert "skill-one" in reply


def test_execute_skill_command_remove(tmp_path):
    create_skill(tmp_path, name="removable", description="test")
    reply = execute_skill_command({"op": "remove", "name": "removable"}, tmp_path)
    assert "removed" in reply.lower()


def test_execute_skill_command_show(tmp_path):
    create_skill(tmp_path, name="show me", description="a test skill")
    reply = execute_skill_command({"op": "show", "name": "show-me"}, tmp_path)
    assert "show-me" in reply or "show me" in reply.lower() or "SKILL.md" in reply or "a test skill" in reply


# ---------------------------------------------------------------------------
# build_blended_signal — no active skills passthrough
# ---------------------------------------------------------------------------

def test_build_blended_signal_no_skills(tmp_path):
    base = _make_signal("yes", 0.12)
    result = build_blended_signal(base, data_dir=tmp_path, window="5m",
                                  symbol="BTCUSDT", min_edge=0.07)
    assert result is base


def test_build_blended_signal_with_mocked_skill(tmp_path):
    """With one active skill, signal is blended (may change action)."""
    create_skill(tmp_path, name="test skill", description="rsi strategy")
    set_skill_enabled(tmp_path, "test-skill", enabled=True)

    # Mock fetch_binance_klines to return synthetic data so test is offline
    fake_klines = [
        {"open_time": i, "open": 100.0, "high": 101.0, "low": 99.0,
         "close": 100.0 + i * 0.1, "volume": 10.0}
        for i in range(60)
    ]
    base = _make_signal("yes", 0.1, 0.7)
    with patch("btc_skill_stack.fetch_binance_klines", return_value=fake_klines):
        result = build_blended_signal(base, data_dir=tmp_path, window="5m",
                                      symbol="BTCUSDT", min_edge=0.07)
    # Result is a blended signal
    assert "blended" in result.signal_source
    assert "constituents" in result.metrics
