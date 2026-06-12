"""/adopt — 보유 편입 + recovery_exit 등록 (사람-승인 매매 설계 §3-2)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.infra.redis_streams import STREAM_CONTROL_COMMANDS
from prime_jennie_runtime.kis_gateway.schemas import PortfolioState, Position
from prime_jennie_runtime.position_sheet.schema import PositionSheet
from prime_jennie_runtime.telegram_bot.adoption import (
    ADOPT_HOLD_BDAYS,
    build_adoption_sheet,
    format_exit_rule,
    parse_recovery_pct,
)
from prime_jennie_runtime.telegram_bot.handler import CommandHandler

# 10:00 KST — build_adoption_sheet 의 valid_until(다음날 15:30) validator 통과 시각
FROZEN_NOW = datetime(2026, 6, 12, 1, 0, 0, tzinfo=UTC)


def _config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="t",
        chat_id="1001",
        allowed_chat_ids=["1001"],
        command_min_interval_s=0,
    )


def _handler(redis, *, pool=None, kis=None) -> CommandHandler:
    return CommandHandler(redis, _config(), now_fn=lambda: FROZEN_NOW, pool=pool, kis_client=kis)


class _ExecRecordingConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: object) -> None:
        self.executed.append((sql, args))


class _AdoptPool:
    """resolve_stock fetchrow + persist acquire + sheet_json fetch 셋 다 지원."""

    def __init__(self, by_code=None, by_name=None, sheets=None) -> None:
        self._by_code = by_code or {}
        self._by_name = by_name or {}
        self._sheets = sheets or {}  # sheet_id → sheet_json str
        self.conn = _ExecRecordingConn()

    async def fetchrow(self, sql: str, arg: str):
        if "stock_code = $1" in sql:
            return self._by_code.get(arg)
        if "stock_name = $1" in sql:
            return self._by_name.get(arg)
        return None

    async def fetch(self, sql: str, arg: list[str]):
        if "FROM position_sheets" in sql:
            return [
                {"sheet_id": sid, "sheet_json": self._sheets[sid]}
                for sid in arg
                if sid in self._sheets
            ]
        return []

    def acquire(self):
        conn = self.conn

        @asynccontextmanager
        async def _cm():
            yield conn

        return _cm()


class _FakeKis:
    def __init__(self, positions: list[Position]) -> None:
        self._positions = positions

    async def get_balance(self) -> PortfolioState:
        return PortfolioState(
            positions=self._positions,
            cash_balance=0,
            total_asset=0,
            stock_eval_amount=0,
            position_count=len(self._positions),
            timestamp=FROZEN_NOW,
        )


def _position(code="178320", name="서진시스템", qty=1, avg=80800) -> Position:
    return Position(
        stock_code=code,
        stock_name=name,
        quantity=qty,
        average_buy_price=avg,
        total_buy_amount=avg * qty,
    )


def _seojin_pool() -> _AdoptPool:
    return _AdoptPool(by_name={"서진시스템": {"stock_code": "178320", "stock_name": "서진시스템"}})


# ---------- parse_recovery_pct ----------


def test_parse_recovery_pct():
    assert parse_recovery_pct("-1") == pytest.approx(-0.01)
    assert parse_recovery_pct("-1%") == pytest.approx(-0.01)
    assert parse_recovery_pct("-2.5") == pytest.approx(-0.025)
    assert parse_recovery_pct("0") == 0.0
    assert parse_recovery_pct("1") is None  # 양수 목표는 fixed_tp 영역
    assert parse_recovery_pct("-11") is None  # 스키마 범위 밖
    assert parse_recovery_pct("abc") is None


# ---------- build_adoption_sheet ----------


def test_build_adoption_sheet_passes_schema():
    sheet = build_adoption_sheet(
        ticker="178320",
        avg_price=80800,
        quantity=1,
        recovery_pct=-0.01,
        now=FROZEN_NOW,
        issued_by="telegram:1001",
        hypothesis="테스트",
    )
    assert isinstance(sheet, PositionSheet)
    assert sheet.strategy_tag == "MANUAL_ADOPT"
    types = [r.type for r in sheet.exit.rules]
    assert types[0] == "recovery_exit"  # first_match 최우선
    assert "fixed_sl" in types and "time_stop" in types
    # paper 측정 제외 보장 — MAX_MEASURABLE_HOLD_BDAYS(60) 초과 hold
    ts = next(r for r in sheet.exit.rules if r.type == "time_stop")
    assert ts.value == ADOPT_HOLD_BDAYS
    assert ts.value > 60
    # 직렬화 round-trip (소비자 측 model_validate_json 경로)
    PositionSheet.model_validate_json(sheet.model_dump_json())


# ---------- /adopt handler ----------


@pytest.mark.asyncio
async def test_adopt_echo_without_confirm(fake_redis):
    pool = _seojin_pool()
    h = _handler(fake_redis, pool=pool, kis=_FakeKis([_position()]))
    res = await h.process_command("/adopt", "서진시스템 -1", chat_id="1001")
    assert res.published is None
    assert "79,992" in res.reply  # 발동가 80,800 × 0.99
    assert "확인" in res.reply
    assert pool.conn.executed == []  # 시트 영속 없음
    assert await fake_redis.xrange(STREAM_CONTROL_COMMANDS) == []


@pytest.mark.asyncio
async def test_adopt_with_confirm_persists_and_publishes(fake_redis):
    pool = _seojin_pool()
    h = _handler(fake_redis, pool=pool, kis=_FakeKis([_position()]))
    res = await h.process_command("/adopt", "서진시스템 -1 확인", chat_id="1001")
    assert res.published is not None
    assert res.published.kind == "adopt_position"
    assert res.published.payload["ticker"] == "178320"
    assert res.published.payload["entry_price"] == 80800.0
    assert res.published.payload["quantity"] == 1
    assert len(pool.conn.executed) == 1
    sql, args = pool.conn.executed[0]
    assert "INSERT INTO position_sheets" in sql
    assert args[5] == "MANUAL_ADOPT"
    msgs = await fake_redis.xrange(STREAM_CONTROL_COMMANDS)
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_adopt_rejects_not_held(fake_redis):
    h = _handler(fake_redis, pool=_seojin_pool(), kis=_FakeKis([]))
    res = await h.process_command("/adopt", "서진시스템 -1 확인", chat_id="1001")
    assert res.published is None
    assert "보유" in res.reply


@pytest.mark.asyncio
async def test_adopt_rejects_already_tracked(fake_redis):
    # tracker 의 Redis 영속 키가 이미 이 ticker 를 추적 — 이중 매도 방지 거부
    await fake_redis.set(
        "position_state:ps_20260612_178320_aaaa",
        json.dumps({"sheet_id": "ps_20260612_178320_aaaa", "ticker": "178320"}),
    )
    h = _handler(fake_redis, pool=_seojin_pool(), kis=_FakeKis([_position()]))
    res = await h.process_command("/adopt", "서진시스템 -1 확인", chat_id="1001")
    assert res.published is None
    assert "이미" in res.reply


@pytest.mark.asyncio
async def test_adopt_rejects_invalid_pct(fake_redis):
    h = _handler(fake_redis, pool=_seojin_pool(), kis=_FakeKis([_position()]))
    res = await h.process_command("/adopt", "서진시스템 +5 확인", chat_id="1001")
    assert res.published is None
    assert "-10" in res.reply


@pytest.mark.asyncio
async def test_adopt_usage_on_missing_args(fake_redis):
    h = _handler(fake_redis, pool=_seojin_pool(), kis=_FakeKis([_position()]))
    res = await h.process_command("/adopt", "", chat_id="1001")
    assert "사용법" in res.reply
    assert "list" in res.reply and "cancel" in res.reply


# ---------- format_exit_rule ----------


def test_format_exit_rule_main_types():
    assert "79,992" in format_exit_rule({"type": "recovery_exit", "pct": -0.01}, 80800)
    assert "손절" in format_exit_rule({"type": "fixed_sl", "pct": 0.10}, 80800)
    assert "250거래일" in format_exit_rule(
        {"type": "time_stop", "mode": "hold_days", "value": 250}, 80800
    )
    assert "장 마감" in format_exit_rule({"type": "time_stop", "mode": "eod"}, 80800)
    # 모르는 type / 깨진 params 는 이름 그대로 (crash 금지)
    assert format_exit_rule({"type": "weird_rule"}, 80800) == "weird_rule"
    assert format_exit_rule({"type": "fixed_sl"}, 80800) == "fixed_sl"


# ---------- /adopt list ----------


def _tracked_state_json(sheet_id="ps_20260612_178320_ab12", ticker="178320") -> str:
    return json.dumps(
        {
            "sheet_id": sheet_id,
            "ticker": ticker,
            "entry_price": 80800.0,
            "quantity": 1,
            "entered_at": "2026-06-12T10:00:00+09:00",
        }
    )


@pytest.mark.asyncio
async def test_adopt_list_empty(fake_redis):
    h = _handler(fake_redis, pool=_seojin_pool())
    res = await h.process_command("/adopt", "list", chat_id="1001")
    assert res.published is None
    assert "없습니다" in res.reply


@pytest.mark.asyncio
async def test_adopt_list_shows_rules_from_sheet(fake_redis):
    sheet = build_adoption_sheet(
        ticker="178320",
        avg_price=80800,
        quantity=1,
        recovery_pct=-0.01,
        now=FROZEN_NOW,
        issued_by="telegram:1001",
        hypothesis="t",
    )
    pool = _AdoptPool(
        by_code={"178320": {"stock_code": "178320", "stock_name": "서진시스템"}},
        sheets={sheet.sheet_id: sheet.model_dump_json()},
    )
    await fake_redis.set(
        f"position_state:{sheet.sheet_id}", _tracked_state_json(sheet_id=sheet.sheet_id)
    )
    h = _handler(fake_redis, pool=pool)
    res = await h.process_command("/adopt", "list", chat_id="1001")
    assert "서진시스템(178320)" in res.reply
    assert "79,992" in res.reply  # recovery_exit 발동가
    assert "손절" in res.reply  # fixed_sl 안전장치도 같이 보임
    assert sheet.sheet_id in res.reply


@pytest.mark.asyncio
async def test_adopt_list_without_sheet_row_degrades(fake_redis):
    """시트 행이 없어도 (DB 유실 등) 종목·수량은 보인다 — 가시성 우선."""
    await fake_redis.set("position_state:ps_x", _tracked_state_json(sheet_id="ps_x"))
    h = _handler(fake_redis, pool=_seojin_pool())
    res = await h.process_command("/adopt", "list", chat_id="1001")
    assert "178320" in res.reply
    assert "룰 정보 없음" in res.reply


# ---------- /adopt cancel ----------


@pytest.mark.asyncio
async def test_adopt_cancel_echo_without_confirm(fake_redis):
    await fake_redis.set("position_state:ps_x", _tracked_state_json(sheet_id="ps_x"))
    h = _handler(fake_redis, pool=_seojin_pool())
    res = await h.process_command("/adopt", "cancel 서진시스템", chat_id="1001")
    assert res.published is None
    assert "확인" in res.reply
    assert "청산 관리에서 빠집니다" in res.reply
    assert await fake_redis.xrange(STREAM_CONTROL_COMMANDS) == []


@pytest.mark.asyncio
async def test_adopt_cancel_with_confirm_publishes_untrack(fake_redis):
    await fake_redis.set("position_state:ps_x", _tracked_state_json(sheet_id="ps_x"))
    h = _handler(fake_redis, pool=_seojin_pool())
    res = await h.process_command("/adopt", "cancel 서진시스템 확인", chat_id="1001")
    assert res.published is not None
    assert res.published.kind == "untrack_position"
    assert res.published.payload == {"sheet_id": "ps_x", "ticker": "178320"}
    msgs = await fake_redis.xrange(STREAM_CONTROL_COMMANDS)
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_adopt_cancel_not_tracked(fake_redis):
    h = _handler(fake_redis, pool=_seojin_pool())
    res = await h.process_command("/adopt", "cancel 서진시스템 확인", chat_id="1001")
    assert res.published is None
    assert "추적 중이 아닙니다" in res.reply


@pytest.mark.asyncio
async def test_adopt_cancel_usage_on_missing_args(fake_redis):
    h = _handler(fake_redis, pool=_seojin_pool())
    res = await h.process_command("/adopt", "cancel", chat_id="1001")
    assert res.published is None
    assert "사용법" in res.reply
