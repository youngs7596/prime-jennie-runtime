"""job-worker 핸들러의 휴장일 가드 — 수급·재무 수집이 휴장일에 안 도는지 확인.

2026-08-22 점검에서 종목별 수급 수집에 가드가 없어 4월 이후 여섯 번(5-01·5-05·
5-25·6-03·7-17·8-17) 직전 거래일 값이 휴장일 날짜로 복사돼 들어간 걸 발견했다.
같은 사고를 막는 회귀 테스트다.
"""

from __future__ import annotations

import pytest

from prime_jennie_runtime.jobs import app as jobs_app

# (핸들러 이름, app 모듈에서 monkeypatch 할 수집 함수 이름)
_GUARDED = [
    ("collect_investor_trading", "collect_investor_trading"),
    ("collect_foreign_holding", "collect_foreign_holding"),
    ("collect_quarterly_financials", "collect_quarterly_financials"),
]


def _build(monkeypatch, *, trading_day: bool, calls: list[str]):
    async def _fake_is_trading_day(*_args, **_kwargs) -> bool:
        return trading_day

    monkeypatch.setattr(jobs_app, "is_trading_day_via_gateway", _fake_is_trading_day)

    for _handler_key, func_name in _GUARDED:

        def _make(name: str):
            async def _fake(*_args, **_kwargs) -> None:
                calls.append(name)

            return _fake

        monkeypatch.setattr(jobs_app, func_name, _make(func_name))

    return jobs_app.build_handlers(
        pool=None,
        http=None,
        redis_client=None,
        kis_gateway_url="http://gateway:8000",
        kis_client=None,
        engine=None,
        telegram_config=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler_key", "func_name"), _GUARDED)
async def test_handler_skips_on_non_trading_day(monkeypatch, handler_key, func_name):
    calls: list[str] = []
    handlers = _build(monkeypatch, trading_day=False, calls=calls)
    await handlers[handler_key]()
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler_key", "func_name"), _GUARDED)
async def test_handler_runs_on_trading_day(monkeypatch, handler_key, func_name):
    calls: list[str] = []
    handlers = _build(monkeypatch, trading_day=True, calls=calls)
    await handlers[handler_key]()
    assert calls == [func_name]
