"""Strategy Policy 로더 테스트."""

from __future__ import annotations

import pytest

from prime_jennie_runtime.slow_loop.strategy.policy import load_policy


def test_load_default_policy():
    policy = load_policy()
    assert policy.version == "v3.0.4"


def test_all_four_tags_present():
    policy = load_policy()
    for tag in ("GAP_UP_REBOUND", "SECTOR_MOMENTUM", "EARNINGS_DRIFT", "MEAN_REVERT_RSI"):
        assert policy.has(tag), f"{tag} missing"


def test_each_entry_has_required_fields():
    policy = load_policy()
    for tag in ("GAP_UP_REBOUND", "SECTOR_MOMENTUM", "EARNINGS_DRIFT", "MEAN_REVERT_RSI"):
        entry = policy.get(tag)
        assert 0 < entry.base_pct <= 0.10
        assert entry.max_notional_krw > 0
        # v2 sizing 동등: 모든 전략에 max_notional_pct 정의
        assert entry.max_notional_pct is not None, f"{tag} missing max_notional_pct"
        assert 0 < entry.max_notional_pct <= 0.25
        # 모든 정책은 fixed_sl + time_stop 필수
        rule_types = {r["type"] for r in entry.default_exit_rules}
        assert "fixed_sl" in rule_types, f"{tag} missing fixed_sl"
        assert "time_stop" in rule_types, f"{tag} missing time_stop"


def test_deprecated_rsi_rebound_not_in_policy():
    """RSI_REBOUND는 폐기됨 — 정책에 없어야."""
    policy = load_policy()
    assert not policy.has("RSI_REBOUND")
    with pytest.raises(KeyError):
        policy.get("RSI_REBOUND")


def test_sector_momentum_has_profit_floor():
    """v2 검증 파라미터 확인."""
    policy = load_policy()
    entry = policy.get("SECTOR_MOMENTUM")
    pf = next((r for r in entry.default_exit_rules if r["type"] == "profit_floor"), None)
    assert pf is not None
    assert pf["activate_pct"] == 0.15
    assert pf["floor_pct"] == 0.10


def test_all_strategies_have_default_entry_conditions():
    """v3.0.2 도입 — 모든 strategy 가 호가 안전장치(spread_under_bps) 부착."""
    policy = load_policy()
    for tag in ("GAP_UP_REBOUND", "SECTOR_MOMENTUM", "EARNINGS_DRIFT", "MEAN_REVERT_RSI"):
        entry = policy.get(tag)
        types = {c["type"] for c in entry.default_entry_conditions}
        assert "spread_under_bps" in types, f"{tag} missing spread_under_bps default"
