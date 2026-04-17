"""KIS Gateway 테스트 공용 fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from prime_jennie_runtime.infra.config import KISConfig


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    """tmp_path 하위 토큰 캐시 파일 경로."""
    return tmp_path / "kis_token.json"


@pytest.fixture
def kis_config(token_file: Path) -> KISConfig:
    """테스트용 KISConfig — base_url 은 mock 서버용 임의 값."""
    return KISConfig(
        app_key="TEST_APP_KEY",
        app_secret="TEST_APP_SECRET",
        account_no="12345678",
        account_product_code="01",
        base_url="https://mock-kis.local",
        is_paper=True,
        token_file_path=str(token_file),
        rate_limit_market_per_sec=19,
        rate_limit_trade_per_sec=5,
        circuit_fail_max=20,
        circuit_reset_sec=60,
    )
