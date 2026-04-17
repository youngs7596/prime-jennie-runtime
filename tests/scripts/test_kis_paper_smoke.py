"""KIS paper smoke 스크립트 — 드라이 단위 테스트 (실 API 미호출).

KISApi 를 mock 해서 각 단계 출력을 검증. 실 KIS 호출은 영석 직접.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from prime_jennie_runtime.kis_gateway.kis_api import KISApiError
from prime_jennie_runtime.kis_gateway.schemas import DailyPrice, StockSnapshot
from scripts.kis_paper_smoke import StepResult, main, run


def _snap() -> StockSnapshot:
    return StockSnapshot(
        stock_code="005930",
        price=71000,
        timestamp=datetime(2026, 4, 17, 9, 30, 0),
        volume=100_000,
    )


def _daily() -> list[DailyPrice]:
    return [
        DailyPrice(
            stock_code="005930",
            price_date=datetime(2026, 4, 15).date(),
            open_price=71000,
            high_price=72000,
            low_price=70500,
            close_price=71500,
            volume=1_000_000,
        )
    ]


def _fake_api(snap=None, snap_exc=None, daily=None, daily_exc=None) -> Any:
    api = AsyncMock()
    api.authenticate.return_value = "tok-12345678"
    if snap_exc is not None:
        api.get_snapshot.side_effect = snap_exc
    else:
        api.get_snapshot.return_value = snap or _snap()
    if daily_exc is not None:
        api.get_daily_prices.side_effect = daily_exc
    else:
        api.get_daily_prices.return_value = daily or _daily()
    api.close = AsyncMock()
    return api


@pytest.mark.asyncio
async def test_run_requires_app_key_and_account(monkeypatch):
    # 환경변수 제거
    for k in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"):
        monkeypatch.delenv(k, raising=False)

    from prime_jennie_runtime.infra.config import KISConfig

    def _empty_cfg(*args, **kwargs) -> KISConfig:
        cfg = KISConfig(app_key="", app_secret="", account_no="", is_paper=True)  # type: ignore[call-arg]
        return cfg

    with patch("scripts.kis_paper_smoke.KISConfig", _empty_cfg):
        results = await run("005930", do_order=False)
    assert len(results) == 1
    assert not results[0].ok
    assert "KIS_APP_KEY" in results[0].detail


@pytest.mark.asyncio
async def test_run_refuses_non_paper_mode(monkeypatch):
    from prime_jennie_runtime.infra.config import KISConfig

    def _real_cfg(*args, **kwargs) -> KISConfig:
        return KISConfig(
            app_key="k",
            app_secret="s",
            account_no="1234",
            is_paper=False,
        )  # type: ignore[call-arg]

    with patch("scripts.kis_paper_smoke.KISConfig", _real_cfg):
        results = await run("005930", do_order=False)
    assert len(results) == 1
    assert not results[0].ok
    assert "paper" in results[0].detail


@pytest.mark.asyncio
async def test_run_happy_path_no_order():
    from prime_jennie_runtime.infra.config import KISConfig

    def _ok_cfg(*args, **kwargs) -> KISConfig:
        return KISConfig(
            app_key="k",
            app_secret="s",
            account_no="1234",
            is_paper=True,
        )  # type: ignore[call-arg]

    fake = _fake_api()
    with (
        patch("scripts.kis_paper_smoke.KISConfig", _ok_cfg),
        patch("scripts.kis_paper_smoke.KISApi", return_value=fake),
    ):
        results = await run("005930", do_order=False)

    names = [r.name for r in results]
    assert names == ["authenticate", "snapshot", "daily_prices"]
    assert all(r.ok for r in results)


@pytest.mark.asyncio
async def test_run_authenticate_failure_short_circuits():
    from prime_jennie_runtime.infra.config import KISConfig

    def _ok_cfg(*args, **kwargs) -> KISConfig:
        return KISConfig(
            app_key="k",
            app_secret="s",
            account_no="1234",
            is_paper=True,
        )  # type: ignore[call-arg]

    fake = AsyncMock()
    fake.authenticate.side_effect = KISApiError("invalid credentials")
    fake.close = AsyncMock()

    with (
        patch("scripts.kis_paper_smoke.KISConfig", _ok_cfg),
        patch("scripts.kis_paper_smoke.KISApi", return_value=fake),
    ):
        results = await run("005930", do_order=False)

    assert len(results) == 1
    assert results[0].name == "authenticate"
    assert not results[0].ok


@pytest.mark.asyncio
async def test_run_snapshot_failure_but_daily_still_attempted():
    from prime_jennie_runtime.infra.config import KISConfig

    def _ok_cfg(*args, **kwargs) -> KISConfig:
        return KISConfig(
            app_key="k",
            app_secret="s",
            account_no="1234",
            is_paper=True,
        )  # type: ignore[call-arg]

    fake = _fake_api(snap_exc=KISApiError("rate limit"))
    with (
        patch("scripts.kis_paper_smoke.KISConfig", _ok_cfg),
        patch("scripts.kis_paper_smoke.KISApi", return_value=fake),
    ):
        results = await run("005930", do_order=False)

    names = {r.name: r for r in results}
    assert names["authenticate"].ok
    assert not names["snapshot"].ok
    assert names["daily_prices"].ok  # 독립 단계 — 계속 진행


def test_main_returns_0_when_all_ok(capsys):
    """CLI entrypoint smoke — run() mock."""

    async def _fake_run(ticker: str, do_order: bool, use_cached: bool = False) -> list[StepResult]:
        return [StepResult("stub", True, "ok")]

    with patch("scripts.kis_paper_smoke.run", _fake_run):
        exit_code = main(["--ticker", "005930"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[OK] stub" in out


def test_main_returns_1_when_any_fail(capsys):
    async def _fake_run(ticker: str, do_order: bool, use_cached: bool = False) -> list[StepResult]:
        return [StepResult("a", True), StepResult("b", False, "boom")]

    with patch("scripts.kis_paper_smoke.run", _fake_run):
        exit_code = main([])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "[FAIL] b" in out
    assert "boom" in out
