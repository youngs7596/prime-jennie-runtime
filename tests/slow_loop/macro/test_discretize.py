"""discretize 경계값 전수 테스트 (MG04~MG06, MG15~MG22)."""

from __future__ import annotations

import pytest

from prime_jennie_runtime.slow_loop.macro.discretize import discretize_sync

# =====================================================================
# MG04~MG06: 기본 이산화
# =====================================================================


def test_mg04_073_rounds_up_to_075():
    """MG04: size_multiplier=0.73 → 0.75."""
    assert discretize_sync(0.73, "open") == 0.75


def test_mg05_026_rounds_up_to_050():
    """MG05: size_multiplier=0.26 → 0.50."""
    assert discretize_sync(0.26, "open") == 0.50


def test_mg06_closed_forces_zero():
    """MG06: gate=closed면 size_multiplier 값 무시하고 0.0."""
    assert discretize_sync(0.3, "closed") == 0.0
    assert discretize_sync(1.0, "closed") == 0.0
    assert discretize_sync(0.0, "closed") == 0.0


# =====================================================================
# MG15~MG18: 경계값 (half-open — 경계는 아래 구간에 포함)
# =====================================================================


def test_mg15_exactly_025():
    """MG15: 정확히 0.25 → 0.25."""
    assert discretize_sync(0.25, "open") == 0.25


def test_mg16_exactly_050():
    """MG16: 정확히 0.50 → 0.50."""
    assert discretize_sync(0.50, "open") == 0.50


def test_mg17_exactly_075():
    """MG17: 정확히 0.75 → 0.75."""
    assert discretize_sync(0.75, "open") == 0.75


def test_mg18_exactly_100():
    """MG18: 정확히 1.00 → 1.00."""
    assert discretize_sync(1.0, "open") == 1.0


# =====================================================================
# MG19~MG20: clamp
# =====================================================================


def test_mg19_above_1_clamps_to_1():
    """MG19: 1.2 → 1.0 (clamp)."""
    assert discretize_sync(1.2, "open") == 1.0


def test_mg20_negative_clamps_to_0():
    """MG20: -0.1 → 0.0 (clamp) → open이면 inconsistent 훅 호출."""
    flag = {"fired": False}

    def hook() -> None:
        flag["fired"] = True

    result = discretize_sync(-0.1, "open", inconsistent_hook=hook)
    # clamp 후 0.0이고 open이므로 inconsistent → 0.25로 강제
    assert result == 0.25
    assert flag["fired"] is True


# =====================================================================
# MG21: open + 0.0 모순
# =====================================================================


def test_mg21_open_zero_forced_to_025():
    """MG21: gate=open + size=0.0 모순 → 0.25 강제, 훅 호출."""
    flag = {"fired": False}

    def hook() -> None:
        flag["fired"] = True

    result = discretize_sync(0.0, "open", inconsistent_hook=hook)
    assert result == 0.25
    assert flag["fired"] is True


def test_mg21_open_zero_no_hook_provided():
    """훅이 없어도 (None) 에러 없이 0.25 반환."""
    assert discretize_sync(0.0, "open") == 0.25


def test_mg21_closed_zero_no_hook_fired():
    """closed는 모순 아님 — 훅 호출 안 됨."""
    flag = {"fired": False}

    def hook() -> None:
        flag["fired"] = True

    result = discretize_sync(0.0, "closed", inconsistent_hook=hook)
    assert result == 0.0
    assert flag["fired"] is False


# =====================================================================
# MG22: 0.25 경계 바로 위
# =====================================================================


def test_mg22_just_above_025_goes_to_050():
    """MG22: 0.2500001 → 0.50 (half-open: > 0.25면 상위 구간)."""
    assert discretize_sync(0.2500001, "open") == 0.50


# =====================================================================
# 추가: 기타 경계 근방
# =====================================================================


@pytest.mark.parametrize(
    "x,expected",
    [
        (0.0001, 0.25),
        (0.2499999, 0.25),
        (0.5000001, 0.75),
        (0.7500001, 1.0),
        (0.99, 1.0),
    ],
)
def test_boundary_sweep(x: float, expected: float):
    assert discretize_sync(x, "open") == expected
