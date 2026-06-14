"""/accept — 추천 수락 매수 (시나리오 B 설계, 2026-06-12).

2단계 확인, 확정 수량 보존 (echo 시점 값 그대로 발행), 장 시간 가드,
PAUSE 무관 발행 (차단은 STOP 만 — consumer 측 정책과 대칭).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from prime_jennie_runtime.infra.config import TelegramConfig
from prime_jennie_runtime.kis_gateway.schemas import PortfolioState, StockSnapshot
from prime_jennie_runtime.position_sheet.schema import KST, PositionSheet
from prime_jennie_runtime.telegram_bot.acceptance import (
    compute_accept_quantity,
    load_pending_accept,
)
from prime_jennie_runtime.telegram_bot.control import (
    STATE_KEY_PAUSE,
    STATE_KEY_STOP,
    reco_key,
)
from prime_jennie_runtime.telegram_bot.handler import CommandHandler

# 2026-06-12 (금) 10:00 KST — 장 시간 안
FROZEN_NOW = datetime(2026, 6, 12, 1, 0, 0, tzinfo=UTC)
# 2026-06-13 (토) 10:00 KST — 휴장
SATURDAY = datetime(2026, 6, 13, 1, 0, 0, tzinfo=UTC)

SHEET_ID = "ps_20260612_005930_a3f2"


def _reco_sheet(
    sheet_id: str = SHEET_ID, ticker: str = "005930", *, day: int = 12
) -> PositionSheet:
    gen = datetime(2026, 6, day, 8, 40, tzinfo=KST)
    return PositionSheet(
        sheet_id=sheet_id,
        generated_at=gen,
        valid_until=datetime(2026, 6, day, 15, 0, tzinfo=KST),
        ticker=ticker,
        strategy_tag="SECTOR_MOMENTUM",
        size={
            "base_pct": 0.05,
            "macro_multiplier": 1.0,
            "risk_multiplier": 1.0,
            "final_pct": 0.05,
            "max_notional_krw": 5_000_000,
        },
        entry={
            "trigger": "limit",
            "price": 70000,
            "valid_until": gen + timedelta(hours=1),
            "conditions": [],
        },
        exit={
            "rules": [
                {"type": "fixed_sl", "pct": 0.05},
                {"type": "time_stop", "mode": "eod"},
            ]
        },
        provenance={
            "scout_run_id": "scout_X",
            "scout_code_hash": "sha256:x",
            "scout_hypothesis": "test",
            "macro_state_snapshot": {
                "gate": "open",
                "size_multiplier": 1.0,
                "gate_run_id": "macro_X",
            },
            "strategy_policy_version": "v3.0.1",
            "generated_by": "test",
        },
    )


class _AcceptPool:
    """load_sheet fetchrow + resolve_stock fetchrow + exit rules fetch 지원."""

    def __init__(self, sheets: dict[str, str], by_code: dict | None = None) -> None:
        self._sheets = sheets  # sheet_id → sheet_json str
        self._by_code = by_code or {}

    async def fetchrow(self, sql: str, arg: str):
        if "FROM position_sheets" in sql:
            sj = self._sheets.get(arg)
            return {"sheet_json": sj} if sj is not None else None
        if "stock_code = $1" in sql:
            return self._by_code.get(arg)
        return None

    async def fetch(self, sql: str, arg: list[str]):
        if "FROM position_sheets" in sql:
            return [
                {"sheet_id": sid, "sheet_json": self._sheets[sid]}
                for sid in arg
                if sid in self._sheets
            ]
        return []


class _FakeKis:
    def __init__(
        self,
        *,
        price: int = 70_000,
        cash: int = 10_000_000,
        total_asset: int = 20_000_000,
    ) -> None:
        self._price = price
        self._cash = cash
        self._total = total_asset

    async def get_balance(self) -> PortfolioState:
        return PortfolioState(
            positions=[],
            cash_balance=self._cash,
            total_asset=self._total,
            stock_eval_amount=self._total - self._cash,
            position_count=0,
            timestamp=FROZEN_NOW,
        )

    async def get_snapshot(self, stock_code: str) -> StockSnapshot:
        return StockSnapshot(stock_code=stock_code, price=self._price, timestamp=FROZEN_NOW)


def _config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="t",
        chat_id="1001",
        allowed_chat_ids=["1001"],
        command_min_interval_s=0,
    )


def _handler(redis, *, pool=None, kis=None, now=FROZEN_NOW) -> CommandHandler:
    return CommandHandler(redis, _config(), now_fn=lambda: now, pool=pool, kis_client=kis)


def _pool_with_sheet(sheet: PositionSheet) -> _AcceptPool:
    return _AcceptPool(
        sheets={sheet.sheet_id: sheet.model_dump_json()},
        by_code={"005930": {"stock_code": "005930", "stock_name": "삼성전자"}},
    )


async def _seed_reco(redis, number: str = "1", sheet_id: str = SHEET_ID, date: str = "2026-06-12"):
    await redis.hset(reco_key(date), mapping={number: sheet_id})


# ---------- compute_accept_quantity ----------


def test_compute_accept_quantity_caps():
    sheet = _reco_sheet()
    # total 20M × 5% = 1M 이 최소 cap → 70,000원 14주
    qty, notional = compute_accept_quantity(
        total_asset=20_000_000, cash_balance=10_000_000, price=70_000, sheet=sheet
    )
    assert (qty, notional) == (14, 1_000_000)
    # 현금이 더 작으면 현금 클램프
    qty, notional = compute_accept_quantity(
        total_asset=20_000_000, cash_balance=500_000, price=70_000, sheet=sheet
    )
    assert (qty, notional) == (7, 500_000)
    # 시세 0 — 0주
    assert compute_accept_quantity(
        total_asset=20_000_000, cash_balance=10_000_000, price=0, sheet=sheet
    ) == (0, 0)


# ---------- /accept 흐름 ----------


@pytest.mark.asyncio
async def test_accept_echo_stores_pending_and_replies_quantity():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await _seed_reco(redis)
        sheet = _reco_sheet()
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis())
        result = await handler.process_command("/accept", "1", 1001)
        assert "삼성전자(005930)" in result.reply
        assert "14주" in result.reply
        assert "시장가" in result.reply
        assert "/accept 1 확인" in result.reply
        assert result.published is None
        pending = await load_pending_accept(redis, "1001")
        assert pending == {
            "number": "1",
            "sheet_id": SHEET_ID,
            "ticker": "005930",
            "quantity": 14,
        }
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_echo_with_quantity_override_limits_to_given_shares():
    """`/accept 번호 수량` — 비중 계산(14주)보다 작은 지정 수량은 그 값으로 제한."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await _seed_reco(redis)
        sheet = _reco_sheet()
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis())
        result = await handler.process_command("/accept", "1 1", 1001)
        assert "1주 × 현재가" in result.reply
        assert "지정 1주 적용" in result.reply
        assert result.published is None
        pending = await load_pending_accept(redis, "1001")
        assert pending["quantity"] == 1
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_echo_quantity_override_caps_at_sizing():
    """지정 수량이 비중 상한(14주)보다 크면 상한으로 묶이고 안내한다."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await _seed_reco(redis)
        sheet = _reco_sheet()
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis())
        result = await handler.process_command("/accept", "1 100", 1001)
        assert "14주 × 현재가" in result.reply
        assert "비중 상한 14주로 제한" in result.reply
        pending = await load_pending_accept(redis, "1001")
        assert pending["quantity"] == 14
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_quantity_override_confirm_publishes_given_shares():
    """echo 에서 1주로 제한한 뒤 확인하면 그 1주로 approved_buy 발행."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(STATE_KEY_PAUSE, b"exit_only_human_approved_v1")
        await _seed_reco(redis)
        sheet = _reco_sheet()
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis())
        await handler.process_command("/accept", "1 1", 1001)
        result = await handler.process_command("/accept", "1 확인", 1001)
        assert result.published is not None
        assert result.published.kind == "approved_buy"
        assert result.published.payload["quantity"] == 1
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_quantity_override_rejects_zero():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await _seed_reco(redis)
        sheet = _reco_sheet()
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis())
        result = await handler.process_command("/accept", "1 0", 1001)
        assert "1주 이상" in result.reply
        assert result.published is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_confirm_without_pending_guides():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await _seed_reco(redis)
        sheet = _reco_sheet()
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis())
        result = await handler.process_command("/accept", "1 확인", 1001)
        assert "확인할 수락 내역이 없습니다" in result.reply
        assert result.published is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_echo_then_confirm_publishes_stored_quantity():
    """확인은 echo 가 보존한 수량 그대로 발행 — 재계산 없음. PAUSE 무관."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(STATE_KEY_PAUSE, b"exit_only_human_approved_v1")
        await _seed_reco(redis)
        sheet = _reco_sheet()
        # echo 시점 시세 70,000 → 14주 보존
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis(price=70_000))
        await handler.process_command("/accept", "1", 1001)
        # 확인 시점 시세가 올라도 (재계산이면 13주) 보존된 14주 그대로
        handler2 = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis(price=75_000))
        result = await handler2.process_command("/accept", "1 확인", 1001)
        assert result.published is not None
        assert result.published.kind == "approved_buy"
        assert result.published.payload == {
            "sheet_id": SHEET_ID,
            "ticker": "005930",
            "quantity": 14,
        }
        assert result.published.reason == "accept_recommendation"
        # pending 은 소진
        assert await load_pending_accept(redis, "1001") is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_confirm_outside_market_hours_blocked():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await _seed_reco(redis, date="2026-06-13")
        sheet = _reco_sheet(day=13)
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis(), now=SATURDAY)
        # 토요일 echo 는 되지만 (장 시간 주의 문구), 확인은 거부
        echo = await handler.process_command("/accept", "1", 1001)
        assert "장 시간이 아닙니다" in echo.reply
        result = await handler.process_command("/accept", "1 확인", 1001)
        assert "장 시간" in result.reply
        assert result.published is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_unknown_number():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        handler = _handler(redis, pool=_AcceptPool({}), kis=_FakeKis())
        result = await handler.process_command("/accept", "9", 1001)
        assert "9번이 없습니다" in result.reply
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_blocked_by_stop():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await redis.set(STATE_KEY_STOP, b"1")
        await _seed_reco(redis)
        sheet = _reco_sheet()
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis())
        result = await handler.process_command("/accept", "1", 1001)
        assert "긴급정지" in result.reply
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_expired_sheet_rejected():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        await _seed_reco(redis)
        sheet = _reco_sheet()
        # 15:00 유효기간 이후 (15:25 KST — 장 시간 안이지만 시트 만료)
        late = datetime(2026, 6, 12, 6, 10, 0, tzinfo=UTC)
        handler = _handler(redis, pool=_pool_with_sheet(sheet), kis=_FakeKis(), now=late)
        result = await handler.process_command("/accept", "1", 1001)
        assert "유효기간" in result.reply
        assert result.published is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_accept_usage_on_bad_args():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        handler = _handler(redis, pool=_AcceptPool({}), kis=_FakeKis())
        result = await handler.process_command("/accept", "", 1001)
        assert "사용법" in result.reply
        result = await handler.process_command("/accept", "abc", 1001)
        assert "사용법" in result.reply
    finally:
        await redis.aclose()
