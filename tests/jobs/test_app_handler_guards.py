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


# ---------- 계약 검사 실패 알림 ----------
# 9-16 부터 21 일 연속 실패가 아무에게도 안 알려져 뉴스·시장 수급이 3 주 멈췄다.


class _FakeBot:
    sent: list[str] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def send_message(self, text: str, **_kwargs) -> bool:
        _FakeBot.sent.append(text)
        return True


def _build_smoke(monkeypatch, smoke):
    from prime_jennie_runtime.telegram_bot import bot as bot_module

    _FakeBot.sent = []
    monkeypatch.setattr(bot_module, "TelegramBot", _FakeBot)
    monkeypatch.setattr(jobs_app, "contract_smoke_test", smoke)
    return jobs_app.build_handlers(
        pool=None,
        http=None,
        redis_client=None,
        kis_gateway_url="http://gateway:8000",
        kis_client=None,
        engine=None,
        telegram_config=object(),
    )


@pytest.mark.asyncio
async def test_contract_smoke_failure_alerts_and_reraises(monkeypatch):
    async def _broken(*_args, **_kwargs) -> None:
        raise jobs_app.ContractSmokeError("1 contract(s) broken: news: <html> 410")

    handlers = _build_smoke(monkeypatch, _broken)
    with pytest.raises(jobs_app.ContractSmokeError):
        await handlers["contract_smoke_test"]()
    [text] = _FakeBot.sent
    assert "계약 검사 실패" in text
    # 텔레그램 HTML 모드라 꺾쇠는 이스케이프돼야 전송이 안 깨진다
    assert "&lt;html&gt;" in text and "<html>" not in text


@pytest.mark.asyncio
async def test_contract_smoke_pass_sends_nothing(monkeypatch):
    async def _ok(*_args, **_kwargs) -> None:
        return None

    handlers = _build_smoke(monkeypatch, _ok)
    await handlers["contract_smoke_test"]()
    assert _FakeBot.sent == []
