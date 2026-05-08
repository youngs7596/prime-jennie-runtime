"""A2/A3/A4/A5 — 확장 명령 핸들러 단위 테스트 (Redis-only / DB / KIS proxy)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.telegram_bot.control import (
    KEY_MAX_BUY_COUNT,
    KEY_MUTE_UNTIL,
    KEY_PRICE_ALERTS,
)
from prime_jennie_runtime.telegram_bot.handler import CommandHandler

FROZEN_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _config(**overrides) -> TelegramConfig:
    base = {
        "bot_token": "t",
        "chat_id": "1001",
        "allowed_chat_ids": ["1001"],
        "command_min_interval_s": 0,
    }
    base.update(overrides)
    return TelegramConfig(**base)


def _handler(redis, *, pool=None, kis=None) -> CommandHandler:
    return CommandHandler(redis, _config(), now_fn=lambda: FROZEN_NOW, pool=pool, kis_client=kis)


# ----- /mute /unmute -----


@pytest.mark.asyncio
async def test_mute_sets_redis_key(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/mute", "30", chat_id="1001")
    assert "30분" in res.reply
    val = await fake_redis.get(KEY_MUTE_UNTIL)
    assert val is not None
    until = int(val.decode())
    assert until > int(time.time())


@pytest.mark.asyncio
async def test_mute_invalid_input(fake_redis):
    h = _handler(fake_redis)
    assert "사용법" in (await h.process_command("/mute", "abc", chat_id="1001")).reply
    assert "1 이상" in (await h.process_command("/mute", "0", chat_id="1001")).reply


@pytest.mark.asyncio
async def test_unmute_clears_key(fake_redis):
    await fake_redis.set(KEY_MUTE_UNTIL, b"99999")
    h = _handler(fake_redis)
    res = await h.process_command("/unmute", "", chat_id="1001")
    assert "재개" in res.reply
    assert await fake_redis.get(KEY_MUTE_UNTIL) is None


# ----- /alert /alerts -----


class _StubPool:
    def __init__(self, by_code=None, by_name=None):
        self._by_code = by_code or {}
        self._by_name = by_name or {}

    async def fetchrow(self, sql, arg):
        if "stock_code = $1" in sql:
            return self._by_code.get(arg)
        if "stock_name = $1" in sql:
            return self._by_name.get(arg)
        return None


@pytest.mark.asyncio
async def test_alert_with_known_code(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/alert", "005930 70000", chat_id="1001")
    assert "70,000" in res.reply
    raw = await fake_redis.hget(KEY_PRICE_ALERTS, "005930:70000")
    assert raw is not None
    data = json.loads(raw.decode())
    assert data["target_price"] == 70000
    assert data["stock_name"] == "삼성전자"


@pytest.mark.asyncio
async def test_alert_invalid_price(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/alert", "005930 abc", chat_id="1001")
    assert "숫자" in res.reply


@pytest.mark.asyncio
async def test_alert_unknown_name(fake_redis):
    pool = _StubPool()
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/alert", "없는 70000", chat_id="1001")
    assert "찾을 수 없" in res.reply


@pytest.mark.asyncio
async def test_alerts_lists_set_alerts(fake_redis):
    await fake_redis.hset(
        KEY_PRICE_ALERTS,
        "005930:70000",
        json.dumps(
            {"stock_code": "005930", "stock_name": "삼성전자", "target_price": 70000}
        ).encode(),
    )
    h = _handler(fake_redis)
    res = await h.process_command("/alerts", "", chat_id="1001")
    assert "삼성전자" in res.reply
    assert "70,000" in res.reply


@pytest.mark.asyncio
async def test_alerts_empty(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/alerts", "", chat_id="1001")
    assert "없습니다" in res.reply


# ----- /maxbuy /config -----


@pytest.mark.asyncio
async def test_maxbuy_sets_redis(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/maxbuy", "5", chat_id="1001")
    assert "5회" in res.reply
    val = await fake_redis.get(KEY_MAX_BUY_COUNT)
    assert val.decode() == "5"


@pytest.mark.asyncio
async def test_maxbuy_out_of_range(fake_redis):
    h = _handler(fake_redis)
    assert "0~20" in (await h.process_command("/maxbuy", "100", chat_id="1001")).reply
    assert "0~20" in (await h.process_command("/maxbuy", "-1", chat_id="1001")).reply


@pytest.mark.asyncio
async def test_config_aggregates_state(fake_redis):
    await fake_redis.set("control.state:dryrun", b"1")
    await fake_redis.set(KEY_MAX_BUY_COUNT, b"7")
    h = _handler(fake_redis)
    res = await h.process_command("/config", "", chat_id="1001")
    assert "DRY_RUN: ON" in res.reply
    assert "7회" in res.reply


# ----- /watch /unwatch /watchlist -----


@pytest.mark.asyncio
async def test_watch_adds_to_redis_hash(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/watch", "005930", chat_id="1001")
    assert "추가" in res.reply
    val = await fake_redis.hget("watchlist:manual", b"005930")
    assert val == b"\xec\x82\xbc\xec\x84\xb1\xec\xa0\x84\xec\x9e\x90"  # UTF-8 of 삼성전자


@pytest.mark.asyncio
async def test_watchlist_lists_all(fake_redis):
    await fake_redis.hset("watchlist:manual", b"005930", "삼성전자".encode())
    await fake_redis.hset("watchlist:manual", b"035720", "카카오".encode())
    h = _handler(fake_redis)
    res = await h.process_command("/watchlist", "", chat_id="1001")
    assert "2종목" in res.reply
    assert "삼성전자" in res.reply
    assert "카카오" in res.reply


@pytest.mark.asyncio
async def test_watchlist_empty(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/watchlist", "", chat_id="1001")
    assert "비어있습니다" in res.reply


@pytest.mark.asyncio
async def test_unwatch_removes(fake_redis):
    await fake_redis.hset("watchlist:manual", b"005930", "삼성전자".encode())
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/unwatch", "005930", chat_id="1001")
    assert "제거" in res.reply
    assert await fake_redis.hget("watchlist:manual", b"005930") is None


@pytest.mark.asyncio
async def test_unwatch_not_in_list(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/unwatch", "005930", chat_id="1001")
    assert "없습니다" in res.reply


# ----- /balance /price /portfolio (KIS gateway proxy) -----


class _StubKis:
    def __init__(self, *, cash=None, snapshot=None, balance=None, raises=None):
        self._cash = cash
        self._snapshot = snapshot
        self._balance = balance
        self._raises = raises or {}

    async def get_cash(self):
        if "get_cash" in self._raises:
            raise self._raises["get_cash"]
        return self._cash

    async def get_snapshot(self, code):
        if "get_snapshot" in self._raises:
            raise self._raises["get_snapshot"]
        return self._snapshot

    async def get_balance(self):
        if "get_balance" in self._raises:
            raise self._raises["get_balance"]
        return self._balance


@pytest.mark.asyncio
async def test_balance_returns_cash(fake_redis):
    h = _handler(fake_redis, kis=_StubKis(cash=15_300_000))
    res = await h.process_command("/balance", "", chat_id="1001")
    assert "15,300,000" in res.reply


@pytest.mark.asyncio
async def test_balance_kis_missing(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/balance", "", chat_id="1001")
    assert "미설정" in res.reply


@pytest.mark.asyncio
async def test_price_with_known_code(fake_redis):
    from prime_jennie_runtime.kis_gateway.schemas import StockSnapshot

    snap = StockSnapshot(
        stock_code="005930",
        price=71200,
        open_price=70000,
        high_price=71500,
        low_price=69800,
        change_pct=1.71,
        timestamp=FROZEN_NOW,
    )
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool, kis=_StubKis(snapshot=snap))
    res = await h.process_command("/price", "005930", chat_id="1001")
    assert "삼성전자" in res.reply
    assert "71,200" in res.reply
    assert "+1.71%" in res.reply


@pytest.mark.asyncio
async def test_price_unknown_stock(fake_redis):
    pool = _StubPool()
    h = _handler(fake_redis, pool=pool, kis=_StubKis())
    res = await h.process_command("/price", "없는종목", chat_id="1001")
    assert "찾을 수 없" in res.reply


@pytest.mark.asyncio
async def test_portfolio_lists_positions(fake_redis):
    from prime_jennie_runtime.kis_gateway.schemas import PortfolioState, Position

    state = PortfolioState(
        positions=[
            Position(
                stock_code="005930",
                stock_name="삼성전자",
                quantity=10,
                average_buy_price=70000,
                total_buy_amount=700000,
                profit_pct=2.5,
            ),
        ],
        cash_balance=10_000_000,
        total_asset=10_700_000,
        stock_eval_amount=700_000,
        position_count=1,
        timestamp=FROZEN_NOW,
    )
    h = _handler(fake_redis, kis=_StubKis(balance=state))
    res = await h.process_command("/portfolio", "", chat_id="1001")
    assert "삼성전자" in res.reply
    assert "1종목" in res.reply
    assert "+2.5%" in res.reply
    assert "10,700,000" in res.reply


@pytest.mark.asyncio
async def test_portfolio_empty(fake_redis):
    from prime_jennie_runtime.kis_gateway.schemas import PortfolioState

    state = PortfolioState(
        positions=[],
        cash_balance=33_070_000,
        total_asset=33_070_000,
        stock_eval_amount=0,
        position_count=0,
        timestamp=FROZEN_NOW,
    )
    h = _handler(fake_redis, kis=_StubKis(balance=state))
    res = await h.process_command("/portfolio", "", chat_id="1001")
    assert "없습니다" in res.reply


# ----- /pnl /diagnose (PG) -----


class _PgPool:
    def __init__(self, rows=None, fetchval_result=1, fetch_raises=None):
        self._rows = rows or []
        self._fetchval_result = fetchval_result
        self._fetch_raises = fetch_raises

    async def fetch(self, sql, *args):
        if self._fetch_raises:
            raise self._fetch_raises
        return self._rows

    async def fetchval(self, sql, *args):
        return self._fetchval_result

    async def fetchrow(self, sql, arg):
        return None


@pytest.mark.asyncio
async def test_pnl_aggregates_today(fake_redis):
    pool = _PgPool(
        rows=[
            {"exit_reason": "tp", "pnl_pct": 2.5, "pnl_krw": 50000},
            {"exit_reason": "sl", "pnl_pct": -1.0, "pnl_krw": -20000},
            {"exit_reason": "tp", "pnl_pct": 1.5, "pnl_krw": 30000},
        ]
    )
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/pnl", "", chat_id="1001")
    assert "3" in res.reply
    assert "승 2 / 패 1" in res.reply
    assert "+1.00%" in res.reply  # avg
    assert "+60,000" in res.reply


@pytest.mark.asyncio
async def test_pnl_no_outcomes(fake_redis):
    pool = _PgPool(rows=[])
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/pnl", "", chat_id="1001")
    assert "없습니다" in res.reply


@pytest.mark.asyncio
async def test_pnl_pool_missing(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/pnl", "", chat_id="1001")
    assert "미주입" in res.reply


@pytest.mark.asyncio
async def test_diagnose_all_ok(fake_redis):
    pool = _PgPool(fetchval_result=1)
    kis = _StubKis(cash=1000)
    h = _handler(fake_redis, pool=pool, kis=kis)
    res = await h.process_command("/diagnose", "", chat_id="1001")
    assert "Redis: OK" in res.reply
    assert "DB: OK" in res.reply
    assert "KIS Gateway: OK" in res.reply


@pytest.mark.asyncio
async def test_diagnose_kis_fail(fake_redis):
    pool = _PgPool(fetchval_result=1)
    kis = _StubKis(raises={"get_cash": RuntimeError("kis down")})
    h = _handler(fake_redis, pool=pool, kis=kis)
    res = await h.process_command("/diagnose", "", chat_id="1001")
    assert "KIS Gateway: FAIL" in res.reply


@pytest.mark.asyncio
async def test_report_aliases_diagnose(fake_redis):
    pool = _PgPool(fetchval_result=1)
    kis = _StubKis(cash=1000)
    h = _handler(fake_redis, pool=pool, kis=kis)
    res = await h.process_command("/report", "", chat_id="1001")
    assert "시스템 진단" in res.reply


# ----- /liquidate sub-commands (A6 확장) -----


@pytest.mark.asyncio
async def test_liquidate_add_publishes(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/liquidate", "add 005930", chat_id="1001")
    assert "추가" in res.reply
    assert res.published is not None
    assert res.published.kind == "liquidate_add"
    assert res.published.payload["ticker"] == "005930"


@pytest.mark.asyncio
async def test_liquidate_remove_publishes(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/liquidate", "remove 005930", chat_id="1001")
    assert "제거" in res.reply
    assert res.published.kind == "liquidate_remove"


@pytest.mark.asyncio
async def test_liquidate_clear_publishes(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/liquidate", "clear", chat_id="1001")
    assert "초기화" in res.reply
    assert res.published.kind == "liquidate_clear"


@pytest.mark.asyncio
async def test_liquidate_list_empty(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/liquidate", "list", chat_id="1001")
    assert "없습니다" in res.reply


@pytest.mark.asyncio
async def test_liquidate_list_with_members(fake_redis):
    await fake_redis.sadd("forced_liquidation:stocks", b"005930")
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/liquidate", "list", chat_id="1001")
    assert "삼성전자" in res.reply


@pytest.mark.asyncio
async def test_liquidate_arm_blocks_when_empty(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/liquidate", "arm", chat_id="1001")
    assert "대상 종목이 없습니다" in res.reply
    assert res.published is None


@pytest.mark.asyncio
async def test_liquidate_arm_with_members_publishes(fake_redis):
    await fake_redis.sadd("forced_liquidation:stocks", b"005930")
    h = _handler(fake_redis)
    res = await h.process_command("/liquidate", "arm", chat_id="1001")
    assert "armed" in res.reply
    assert res.published.kind == "liquidate_arm"


# ----- /buy /sell /sellall (A7 manual trading) -----


@pytest.mark.asyncio
async def test_buy_with_explicit_quantity(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/buy", "005930 10", chat_id="1001")
    assert "매수 요청 발행" in res.reply
    assert "10주" in res.reply
    assert res.published.kind == "manual_buy"
    assert res.published.payload == {"ticker": "005930", "quantity": 10}


@pytest.mark.asyncio
async def test_buy_auto_quantity_uses_kis(fake_redis):
    from prime_jennie_runtime.kis_gateway.schemas import StockSnapshot

    snap = StockSnapshot(stock_code="005930", price=70000, timestamp=FROZEN_NOW)
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    kis = _StubKis(cash=10_000_000, snapshot=snap)
    h = _handler(fake_redis, pool=pool, kis=kis)
    # 1천만 × 0.20 / 70000 = 28주
    res = await h.process_command("/buy", "005930", chat_id="1001")
    assert "28주" in res.reply


@pytest.mark.asyncio
async def test_buy_unknown_stock(fake_redis):
    h = _handler(fake_redis, pool=_StubPool())
    res = await h.process_command("/buy", "없는종목 10", chat_id="1001")
    assert "찾을 수 없" in res.reply
    assert res.published is None


@pytest.mark.asyncio
async def test_sell_explicit_quantity(fake_redis):
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/sell", "005930 5", chat_id="1001")
    assert "매도 요청 발행" in res.reply
    assert res.published.kind == "manual_sell"
    assert res.published.payload == {"ticker": "005930", "quantity": 5}


@pytest.mark.asyncio
async def test_sell_full_uses_kis_position(fake_redis):
    from prime_jennie_runtime.kis_gateway.schemas import PortfolioState, Position

    state = PortfolioState(
        positions=[
            Position(
                stock_code="005930",
                stock_name="삼성전자",
                quantity=15,
                average_buy_price=70000,
                total_buy_amount=1_050_000,
            )
        ],
        cash_balance=0,
        total_asset=0,
        stock_eval_amount=0,
        position_count=1,
        timestamp=FROZEN_NOW,
    )
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool, kis=_StubKis(balance=state))
    res = await h.process_command("/sell", "005930 전량", chat_id="1001")
    assert res.published.payload["quantity"] == 15


@pytest.mark.asyncio
async def test_sell_full_no_position(fake_redis):
    from prime_jennie_runtime.kis_gateway.schemas import PortfolioState

    state = PortfolioState(
        positions=[],
        cash_balance=0,
        total_asset=0,
        stock_eval_amount=0,
        position_count=0,
        timestamp=FROZEN_NOW,
    )
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool, kis=_StubKis(balance=state))
    res = await h.process_command("/sell", "005930 전량", chat_id="1001")
    assert "보유하고 있지 않습니다" in res.reply
    assert res.published is None


@pytest.mark.asyncio
async def test_sellall_requires_confirmation(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/sellall", "", chat_id="1001")
    assert "확인" in res.reply
    assert res.published is None


@pytest.mark.asyncio
async def test_sellall_with_confirmation(fake_redis):
    h = _handler(fake_redis)
    res = await h.process_command("/sellall", "확인", chat_id="1001")
    assert "전체 청산 요청 발행" in res.reply
    assert res.published.kind == "manual_sellall"


# ----- STOP/PAUSE 시 수동 매매 차단 (Fix #3) -----


@pytest.mark.asyncio
async def test_buy_blocked_by_stop(fake_redis):
    await fake_redis.set("control.state:stop", b"1")
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/buy", "005930 10", chat_id="1001")
    assert "긴급정지" in res.reply
    assert res.published is None


@pytest.mark.asyncio
async def test_buy_blocked_by_pause(fake_redis):
    await fake_redis.set("control.state:pause", b"manual")
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/buy", "005930 10", chat_id="1001")
    assert "일시정지" in res.reply
    assert res.published is None


@pytest.mark.asyncio
async def test_sell_blocked_by_stop(fake_redis):
    await fake_redis.set("control.state:stop", b"1")
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/sell", "005930 5", chat_id="1001")
    assert "긴급정지" in res.reply
    assert res.published is None


@pytest.mark.asyncio
async def test_sell_allowed_during_pause(fake_redis):
    """PAUSE 는 청산 허용 (docstring '진입만 중단')."""
    await fake_redis.set("control.state:pause", b"manual")
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/sell", "005930 5", chat_id="1001")
    assert "매도 요청 발행" in res.reply
    assert res.published.kind == "manual_sell"


@pytest.mark.asyncio
async def test_sellall_blocked_by_stop(fake_redis):
    await fake_redis.set("control.state:stop", b"1")
    h = _handler(fake_redis)
    res = await h.process_command("/sellall", "확인", chat_id="1001")
    assert "긴급정지" in res.reply
    assert res.published is None


# ----- ControlConsumer 방어적 가드 -----


@pytest.mark.asyncio
async def test_consumer_manual_trade_blocked_by_stop(fake_redis):
    """publish 후 STOP 이 들어와도 consumer 가 막아야 한다."""
    from datetime import UTC
    from datetime import datetime as dt

    from prime_jennie_runtime.control.consumer import ControlCommandConsumer
    from prime_jennie_runtime.telegram_bot.control import ControlCommand

    await fake_redis.set("control.state:stop", b"1")
    kis = _RecordingKis()
    consumer = ControlCommandConsumer(fake_redis, consumer_name="t", kis_client=kis)
    await consumer.apply(
        ControlCommand(
            kind="manual_buy",
            issued_at=dt(2026, 5, 8, tzinfo=UTC),
            issued_by="t",
            payload={"ticker": "005930", "quantity": 10},
        )
    )
    assert kis.calls == []  # KIS 호출 없음


@pytest.mark.asyncio
async def test_consumer_manual_buy_blocked_by_pause(fake_redis):
    from datetime import UTC
    from datetime import datetime as dt

    from prime_jennie_runtime.control.consumer import ControlCommandConsumer
    from prime_jennie_runtime.telegram_bot.control import ControlCommand

    await fake_redis.set("control.state:pause", b"manual")
    kis = _RecordingKis()
    consumer = ControlCommandConsumer(fake_redis, consumer_name="t", kis_client=kis)
    await consumer.apply(
        ControlCommand(
            kind="manual_buy",
            issued_at=dt(2026, 5, 8, tzinfo=UTC),
            issued_by="t",
            payload={"ticker": "005930", "quantity": 10},
        )
    )
    assert kis.calls == []


@pytest.mark.asyncio
async def test_consumer_manual_sell_passes_during_pause(fake_redis):
    """PAUSE 는 청산 허용."""
    from datetime import UTC
    from datetime import datetime as dt

    from prime_jennie_runtime.control.consumer import ControlCommandConsumer
    from prime_jennie_runtime.telegram_bot.control import ControlCommand

    await fake_redis.set("control.state:pause", b"manual")
    kis = _RecordingKis()
    consumer = ControlCommandConsumer(fake_redis, consumer_name="t", kis_client=kis)
    await consumer.apply(
        ControlCommand(
            kind="manual_sell",
            issued_at=dt(2026, 5, 8, tzinfo=UTC),
            issued_by="t",
            payload={"ticker": "005930", "quantity": 10},
        )
    )
    assert len(kis.calls) == 1 and kis.calls[0][0] == "sell"


@pytest.mark.asyncio
async def test_manual_trade_daily_limit(fake_redis):
    h = _handler(fake_redis)
    # MANUAL_TRADE_DAILY_LIMIT 까지는 통과
    pool = _StubPool(by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}})
    h._pool = pool
    for _ in range(20):
        await h.process_command("/buy", "005930 1", chat_id="1001")
    # 21번째는 거부
    res = await h.process_command("/buy", "005930 1", chat_id="1001")
    assert "한도" in res.reply


# ----- ControlConsumer 측 manual trade handler -----


class _RecordingKis:
    def __init__(self, *, balance=None):
        self.calls: list[tuple[str, dict]] = []
        self._balance = balance

    async def buy(self, order):
        from prime_jennie_runtime.kis_gateway.schemas import OrderResult

        self.calls.append(("buy", order.model_dump()))
        return OrderResult(
            success=True,
            order_no="order_123",
            stock_code=order.stock_code,
            quantity=order.quantity,
            price=0,
        )

    async def sell(self, order):
        from prime_jennie_runtime.kis_gateway.schemas import OrderResult

        self.calls.append(("sell", order.model_dump()))
        return OrderResult(
            success=True,
            order_no="order_456",
            stock_code=order.stock_code,
            quantity=order.quantity,
            price=0,
        )

    async def get_balance(self):
        return self._balance


@pytest.mark.asyncio
async def test_consumer_manual_buy_calls_kis(fake_redis):
    from datetime import UTC
    from datetime import datetime as dt

    from prime_jennie_runtime.control.consumer import ControlCommandConsumer
    from prime_jennie_runtime.telegram_bot.control import ControlCommand

    kis = _RecordingKis()
    consumer = ControlCommandConsumer(fake_redis, consumer_name="t", kis_client=kis)
    await consumer.apply(
        ControlCommand(
            kind="manual_buy",
            issued_at=dt(2026, 5, 8, tzinfo=UTC),
            issued_by="t",
            payload={"ticker": "005930", "quantity": 10},
        )
    )
    assert kis.calls == [
        ("buy", {"stock_code": "005930", "quantity": 10, "order_type": "market", "price": 0})
    ]


@pytest.mark.asyncio
async def test_consumer_manual_buy_skipped_when_dryrun(fake_redis):
    from datetime import UTC
    from datetime import datetime as dt

    from prime_jennie_runtime.control.consumer import ControlCommandConsumer
    from prime_jennie_runtime.telegram_bot.control import ControlCommand

    await fake_redis.set("control.state:dryrun", b"1")
    kis = _RecordingKis()
    consumer = ControlCommandConsumer(fake_redis, consumer_name="t", kis_client=kis)
    await consumer.apply(
        ControlCommand(
            kind="manual_buy",
            issued_at=dt(2026, 5, 8, tzinfo=UTC),
            issued_by="t",
            payload={"ticker": "005930", "quantity": 10},
        )
    )
    assert kis.calls == []


@pytest.mark.asyncio
async def test_consumer_manual_sellall_iterates_positions(fake_redis):
    from datetime import UTC
    from datetime import datetime as dt

    from prime_jennie_runtime.control.consumer import ControlCommandConsumer
    from prime_jennie_runtime.kis_gateway.schemas import PortfolioState, Position
    from prime_jennie_runtime.telegram_bot.control import ControlCommand

    state = PortfolioState(
        positions=[
            Position(
                stock_code="005930",
                stock_name="삼성전자",
                quantity=10,
                average_buy_price=70000,
                total_buy_amount=700000,
            ),
            Position(
                stock_code="035720",
                stock_name="카카오",
                quantity=20,
                average_buy_price=50000,
                total_buy_amount=1_000_000,
            ),
        ],
        cash_balance=0,
        total_asset=0,
        stock_eval_amount=0,
        position_count=2,
        timestamp=FROZEN_NOW,
    )
    kis = _RecordingKis(balance=state)
    consumer = ControlCommandConsumer(fake_redis, consumer_name="t", kis_client=kis)
    await consumer.apply(
        ControlCommand(
            kind="manual_sellall",
            issued_at=dt(2026, 5, 8, tzinfo=UTC),
            issued_by="t",
        )
    )
    sell_calls = [c for c in kis.calls if c[0] == "sell"]
    assert len(sell_calls) == 2
    assert {c[1]["stock_code"] for c in sell_calls} == {"005930", "035720"}


@pytest.mark.asyncio
async def test_liquidate_consumer_handlers(fake_redis):
    """ControlConsumer 측 핸들러 — apply 시 forced_liquidation:stocks 갱신."""
    from datetime import UTC
    from datetime import datetime as dt

    from prime_jennie_runtime.control.consumer import ControlCommandConsumer
    from prime_jennie_runtime.telegram_bot.control import ControlCommand

    consumer = ControlCommandConsumer(fake_redis, consumer_name="t")

    await consumer.apply(
        ControlCommand(
            kind="liquidate_add",
            issued_at=dt(2026, 5, 8, tzinfo=UTC),
            issued_by="test",
            payload={"ticker": "005930"},
        )
    )
    members = await fake_redis.smembers("forced_liquidation:stocks")
    assert b"005930" in members

    await consumer.apply(
        ControlCommand(
            kind="liquidate_remove",
            issued_at=dt(2026, 5, 8, tzinfo=UTC),
            issued_by="test",
            payload={"ticker": "005930"},
        )
    )
    members = await fake_redis.smembers("forced_liquidation:stocks")
    assert b"005930" not in members

    await fake_redis.sadd("forced_liquidation:stocks", b"x", b"y")
    await fake_redis.set("control.state:liquidate_armed", b"1")
    await consumer.apply(
        ControlCommand(
            kind="liquidate_clear",
            issued_at=dt(2026, 5, 8, tzinfo=UTC),
            issued_by="test",
        )
    )
    assert not await fake_redis.smembers("forced_liquidation:stocks")
    assert await fake_redis.get("control.state:liquidate_armed") is None
