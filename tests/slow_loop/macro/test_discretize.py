"""discretize 경계값 전수 테스트.

open 이산화는 2단계 (0.75 / 1.0) 로 축소됨 (2026-05-10).
"""

from __future__ import annotations

import pytest

from prime_jennie_runtime.slow_loop.macro.discretize import OPEN_FLOOR, discretize_sync

# =====================================================================
# 기본 이산화
# =====================================================================


def test_073_rounds_up_to_075():
    assert discretize_sync(0.73, "open") == 0.75


def test_026_rounds_up_to_floor():
    """0.26 같은 보수 신호도 open 인 이상 OPEN_FLOOR(0.75)."""
    assert discretize_sync(0.26, "open") == OPEN_FLOOR


def test_closed_forces_zero():
    assert discretize_sync(0.3, "closed") == 0.0
    assert discretize_sync(1.0, "closed") == 0.0
    assert discretize_sync(0.0, "closed") == 0.0


# =====================================================================
# 경계값 (half-open — 경계는 아래 구간에 포함)
# =====================================================================


def test_exactly_025_maps_to_floor():
    assert discretize_sync(0.25, "open") == OPEN_FLOOR


def test_exactly_050_maps_to_floor():
    assert discretize_sync(0.50, "open") == OPEN_FLOOR


def test_exactly_075_stays_floor():
    assert discretize_sync(0.75, "open") == 0.75


def test_exactly_100_stays_one():
    assert discretize_sync(1.0, "open") == 1.0


def test_just_above_075_goes_to_one():
    assert discretize_sync(0.7500001, "open") == 1.0


# =====================================================================
# clamp
# =====================================================================


def test_above_1_clamps_to_1():
    assert discretize_sync(1.2, "open") == 1.0


def test_negative_clamps_to_zero_then_floor():
    """clamp 후 0.0 + open 은 모순 → OPEN_FLOOR + 훅."""
    flag = {"fired": False}

    def hook() -> None:
        flag["fired"] = True

    result = discretize_sync(-0.1, "open", inconsistent_hook=hook)
    assert result == OPEN_FLOOR
    assert flag["fired"] is True


# =====================================================================
# open + 0.0 모순
# =====================================================================


def test_open_zero_forced_to_floor():
    flag = {"fired": False}

    def hook() -> None:
        flag["fired"] = True

    result = discretize_sync(0.0, "open", inconsistent_hook=hook)
    assert result == OPEN_FLOOR
    assert flag["fired"] is True


def test_open_zero_no_hook_provided():
    """훅이 없어도 (None) 에러 없이 OPEN_FLOOR 반환."""
    assert discretize_sync(0.0, "open") == OPEN_FLOOR


def test_closed_zero_no_hook_fired():
    """closed 는 모순 아님 — 훅 호출 안 됨."""
    flag = {"fired": False}

    def hook() -> None:
        flag["fired"] = True

    result = discretize_sync(0.0, "closed", inconsistent_hook=hook)
    assert result == 0.0
    assert flag["fired"] is False


# =====================================================================
# 추가: 기타 경계 근방
# =====================================================================


@pytest.mark.parametrize(
    "x,expected",
    [
        (0.0001, OPEN_FLOOR),
        (0.2499999, OPEN_FLOOR),
        (0.5000001, OPEN_FLOOR),
        (0.7500001, 1.0),
        (0.99, 1.0),
    ],
)
def test_boundary_sweep(x: float, expected: float):
    assert discretize_sync(x, "open") == expected
