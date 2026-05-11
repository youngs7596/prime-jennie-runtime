"""KIS Gateway paper/real 모드 안전장치 테스트."""

from __future__ import annotations

import logging

import pytest

from prime_jennie_runtime.infra.config import KISConfig
from prime_jennie_runtime.kis_gateway.mode_safety import (
    REAL_CONFIRM_ENV,
    REAL_CONFIRM_VALUE,
    enforce_mode_safety,
)


def _cfg(*, is_paper: bool, account_no: str = "50012345") -> KISConfig:
    return KISConfig(
        app_key="k",
        app_secret="s",
        account_no=account_no,
        account_product_code="01",
        base_url="https://mock-kis.local",
        is_paper=is_paper,
    )


def test_paper_mode_passes_without_confirm_env():
    """paper 모드는 confirm 환경변수 없이도 부팅 통과."""
    enforce_mode_safety(_cfg(is_paper=True), env={})


def test_real_mode_without_confirm_raises():
    """real 모드 + confirm 환경변수 누락 → RuntimeError."""
    with pytest.raises(RuntimeError, match="REAL trading mode"):
        enforce_mode_safety(_cfg(is_paper=False, account_no="12345678"), env={})


def test_real_mode_with_wrong_confirm_raises():
    """real 모드 + confirm 환경변수 값 잘못 → RuntimeError."""
    with pytest.raises(RuntimeError, match="REAL trading mode"):
        enforce_mode_safety(
            _cfg(is_paper=False, account_no="12345678"),
            env={REAL_CONFIRM_ENV: "yes"},
        )


def test_real_mode_with_correct_confirm_passes(caplog):
    """real 모드 + 정확한 confirm 값 → 통과 + REAL 배너 로깅."""
    caplog.set_level(logging.INFO, logger="prime_jennie_runtime.kis_gateway.mode_safety")
    enforce_mode_safety(
        _cfg(is_paper=False, account_no="12345678"),
        env={REAL_CONFIRM_ENV: REAL_CONFIRM_VALUE},
    )
    assert any("REAL" in rec.message for rec in caplog.records)


def test_paper_mode_logs_paper_banner(caplog):
    """paper 모드는 PAPER 배너 INFO 로깅."""
    caplog.set_level(logging.INFO, logger="prime_jennie_runtime.kis_gateway.mode_safety")
    enforce_mode_safety(_cfg(is_paper=True, account_no="50012345"), env={})
    assert any("PAPER" in rec.message for rec in caplog.records)


def test_paper_mode_warns_on_real_account_pattern(caplog):
    """paper 모드인데 계좌번호가 '5' 로 시작하지 않으면 경고 로그."""
    caplog.set_level(logging.WARNING, logger="prime_jennie_runtime.kis_gateway.mode_safety")
    enforce_mode_safety(_cfg(is_paper=True, account_no="12345678"), env={})
    assert any(
        "may be a real account" in rec.message and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_paper_mode_no_warning_for_paper_account(caplog):
    """paper 모드 + '5' 시작 계좌번호는 경고 없음."""
    caplog.set_level(logging.WARNING, logger="prime_jennie_runtime.kis_gateway.mode_safety")
    enforce_mode_safety(_cfg(is_paper=True, account_no="50012345"), env={})
    assert not any("may be a real account" in rec.message for rec in caplog.records)
