"""실보유 종목의 v3 관리 편입 — 사람-승인 매매 설계 §3-2.

시트 만료 등으로 PositionTracker 밖에 있는 실계좌 보유 종목을 관리용 시트
발행 + tracker 등록으로 v3 청산 관리 안에 들여온다. 2026-05-21 KODEX200·
두산밥캣 수동 편입 (ps_20260521_*) 전례의 명령화.

흐름 (telegram /adopt → fast-loop):
1. telegram handler 가 KIS 잔고에서 매입가·수량 확인
2. 관리용 시트 구성 (사용자 recovery_exit + 안전장치 fixed_sl/time_stop)
   + ``position_sheets`` INSERT — paper 측정은 hold_days(250) > 60 제외 필터가
   자동으로 걸러내므로 측정 오염 없음 (jobs/paper_outcomes.py)
3. ``control.commands`` 로 adopt_position 발행 — fast-loop 의
   ControlCommandConsumer 가 in-process PositionTracker 에 등록. 외부에서
   Redis 키만 쓰면 fast-loop 재기동 전까지 in-memory tracker 가 모른다.

티커 구독은 별도 작업 불필요 — positions 테이블 종목은 fast-loop 기동 시
전부 gateway 에 구독된다 (fast_loop/gateway_subscriber.py).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time, timedelta
from typing import Any

from prime_jennie_runtime.fast_loop.position_tracker import STATE_KEY_PREFIX
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    EntrySection,
    ExitSection,
    FixedSlRule,
    MacroStateSnapshot,
    PositionSheet,
    ProvenanceSection,
    RecoveryExitRule,
    SizeSection,
    TimeStopRule,
)

logger = logging.getLogger(__name__)

# 측정 제외 필터 (MAX_MEASURABLE_HOLD_BDAYS=60) 를 확실히 넘는 placeholder —
# 5-21 수동 편입과 동일 값. 250 거래일 뒤 time_stop 이 실제로 닫는 안전망 겸용.
ADOPT_HOLD_BDAYS = 250
# 매입가 대비 재난 손절 — 스키마 허용 최대 (10%). 편입 확인 메시지에 명시해
# 사용자가 인지한 상태로 등록된다.
ADOPT_FIXED_SL_PCT = 0.10


def parse_recovery_pct(raw: str) -> float | None:
    """사용자 입력 회복선(%) → 분수. 범위 밖이거나 숫자가 아니면 None.

    "-1" / "-1%" / "-1.5" / "0" → -0.01 / -0.01 / -0.015 / 0.0.
    recovery_exit 스키마 범위 (-10% ~ 0%) 그대로.
    """
    s = raw.strip().rstrip("%")
    try:
        value = float(s)
    except ValueError:
        return None
    pct = value / 100.0
    if pct < -0.10 or pct > 0.0:
        return None
    return pct


def build_adoption_sheet(
    *,
    ticker: str,
    avg_price: int,
    quantity: int,
    recovery_pct: float,
    now: datetime,
    issued_by: str,
    hypothesis: str,
) -> PositionSheet:
    """편입 관리 시트 구성 — 스키마 validator 전체 통과를 보장.

    valid_until 은 다음날 15:30 KST — 시각 validator (15:30 이하 + generated_at
    이후 60초) 를 장중·장외 어느 시점 편입이든 만족시키는 값. 진입은 이미 끝난
    보유라 entry 섹션은 형식 충족용이고, exit 경로는 valid_until 을 보지 않는다.
    """
    gen = now.astimezone(KST)
    valid_until = datetime.combine(gen.date() + timedelta(days=1), time(15, 30, tzinfo=KST))
    sheet_id = f"ps_{gen:%Y%m%d}_{ticker}_{uuid.uuid4().hex[:4]}"
    return PositionSheet(
        sheet_id=sheet_id,
        generated_at=gen,
        valid_until=valid_until,
        ticker=ticker,
        strategy_tag="MANUAL_ADOPT",
        size=SizeSection(
            base_pct=0.05,
            macro_multiplier=1.0,
            risk_multiplier=1.0,
            final_pct=0.05,
            max_notional_krw=max(int(avg_price) * int(quantity), 1),
        ),
        entry=EntrySection(
            trigger="market",
            price=None,
            valid_until=gen + timedelta(seconds=60),
        ),
        exit=ExitSection(
            rules=[
                RecoveryExitRule(pct=recovery_pct),
                FixedSlRule(pct=ADOPT_FIXED_SL_PCT),
                TimeStopRule(mode="hold_days", value=ADOPT_HOLD_BDAYS),
            ],
        ),
        provenance=ProvenanceSection(
            scout_run_id=f"adopt_{gen:%Y%m%d}",
            scout_code_hash="manual_adoption",
            scout_hypothesis=hypothesis,
            macro_state_snapshot=MacroStateSnapshot(
                gate="open",
                size_multiplier=1.0,
                gate_run_id=f"adopt_{gen:%Y%m%d}",
            ),
            strategy_policy_version="manual_adopt_v1",
            generated_by=issued_by,
        ),
    )


async def persist_adoption_sheet(pool: Any, sheet: PositionSheet) -> None:
    """``position_sheets`` INSERT — slow_loop publisher 의 upsert 와 동일 컬럼.

    telegram-bot 은 asyncpg pool 이라 publisher (sqlalchemy engine) 를 재사용하지
    않고 같은 SQL 을 직접 친다. ON CONFLICT DO NOTHING — sheet_id 는 uuid 꼬리라
    충돌 자체가 사실상 없음.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO position_sheets (
                sheet_id, schema_version, generated_at, valid_until,
                ticker, strategy_tag, sheet_json, provenance_json
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
            ON CONFLICT (sheet_id) DO NOTHING
            """,
            sheet.sheet_id,
            sheet.schema_version,
            sheet.generated_at,
            sheet.valid_until,
            sheet.ticker,
            sheet.strategy_tag,
            sheet.model_dump_json(),
            sheet.provenance.model_dump_json(),
        )


async def scan_tracked_states(redis_client: Any) -> list[dict[str, Any]]:
    """tracker 의 Redis 영속 state 전체 → dict 리스트 (sheet_id 정렬).

    telegram-bot 프로세스에서 fast-loop in-memory tracker 를 볼 수 없어
    `position_state:{sheet_id}` 키를 직접 훑는다. 편입 중복 가드 + 조회(/adopt
    list) + 취소(/adopt cancel) 의 공용 데이터 소스.
    """
    states: list[dict[str, Any]] = []
    cursor = 0
    while True:
        cursor, keys = await redis_client.scan(cursor, match=f"{STATE_KEY_PREFIX}*", count=100)
        for raw_key in keys:
            raw = await redis_client.get(raw_key)
            if raw is None:
                continue
            try:
                data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(data, dict) and data.get("sheet_id"):
                states.append(data)
        if cursor == 0:
            break
    states.sort(key=lambda d: str(d.get("sheet_id", "")))
    return states


async def find_tracked_state_for_ticker(redis_client: Any, ticker: str) -> dict[str, Any] | None:
    """이 ticker 를 추적 중인 tracker state dict. 없으면 None."""
    for data in await scan_tracked_states(redis_client):
        if data.get("ticker") == ticker:
            return data
    return None


async def find_tracked_sheet_for_ticker(redis_client: Any, ticker: str) -> str | None:
    """이 ticker 를 이미 추적 중인 tracker state 가 있으면 그 sheet_id.

    이중 편입(같은 종목 시트 2장 → 발동 시 이중 매도) 방지 가드.
    """
    state = await find_tracked_state_for_ticker(redis_client, ticker)
    return str(state["sheet_id"]) if state else None


async def load_sheet_exit_rules(pool: Any, sheet_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """``position_sheets`` 에서 sheet_id 별 exit 룰 (raw dict 리스트) 로드.

    룰의 정본은 sheet_json — tracker state 에는 룰이 없다. 행이 없거나 파싱에
    실패한 sheet_id 는 결과에서 빠진다 (호출자가 "룰 정보 없음" 으로 표시).
    """
    if not sheet_ids:
        return {}
    rows = await pool.fetch(
        "SELECT sheet_id, sheet_json FROM position_sheets WHERE sheet_id = ANY($1::text[])",
        sheet_ids,
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw = row["sheet_json"]
        try:
            data = json.loads(raw) if isinstance(raw, str | bytes) else raw
            rules = data["exit"]["rules"]
        except (TypeError, KeyError, ValueError):
            continue
        if isinstance(rules, list):
            out[str(row["sheet_id"])] = [r for r in rules if isinstance(r, dict)]
    return out


def format_exit_rule(rule: dict[str, Any], entry_price: float) -> str:
    """exit 룰 1건 → 사용자용 한국어 한 줄. 모르는 type 은 이름 그대로."""
    t = str(rule.get("type", "?"))
    try:
        if t == "recovery_exit":
            pct = float(rule["pct"])
            trigger = int(entry_price * (1 + pct))
            return f"손익률 {pct * 100:.1f}% 이상 회복 시 전량 매도 (발동가 약 {trigger:,}원)"
        if t == "fixed_sl":
            pct = float(rule["pct"])
            return f"매입가 대비 -{pct * 100:.1f}% 손절 (약 {int(entry_price * (1 - pct)):,}원)"
        if t == "fixed_tp":
            pct = float(rule["pct"])
            return f"+{pct * 100:.1f}% 익절 (약 {int(entry_price * (1 + pct)):,}원)"
        if t == "time_stop":
            if rule.get("mode") == "hold_days":
                return f"{int(rule.get('value') or 0)}거래일 경과 시 종료"
            return "당일 장 마감 시 종료"
        if t == "trailing_tp":
            return (
                f"+{float(rule['activate_pct']) * 100:.1f}% 도달 후 고점 대비 "
                f"-{float(rule['drop_pct']) * 100:.1f}% 밀리면 매도"
            )
        if t in ("breakeven", "profit_floor"):
            label = "본전 방어" if t == "breakeven" else "이익 방어"
            return (
                f"+{float(rule['activate_pct']) * 100:.1f}% 도달 후 "
                f"+{float(rule['floor_pct']) * 100:.1f}% 밑으로 밀리면 매도 ({label})"
            )
        if t == "scale_out":
            return f"부분 청산 {len(rule.get('levels') or [])}단계"
        if t == "death_cross":
            return "데드크로스 (MA5/MA20) 시 매도"
        if t == "overextension_exit":
            return f"1분봉 RSI {float(rule['rsi_threshold']):.0f} 과열 시 매도"
    except (TypeError, ValueError, KeyError):
        pass
    return t


__all__ = [
    "ADOPT_FIXED_SL_PCT",
    "ADOPT_HOLD_BDAYS",
    "build_adoption_sheet",
    "find_tracked_sheet_for_ticker",
    "find_tracked_state_for_ticker",
    "format_exit_rule",
    "load_sheet_exit_rules",
    "parse_recovery_pct",
    "persist_adoption_sheet",
    "scan_tracked_states",
]
