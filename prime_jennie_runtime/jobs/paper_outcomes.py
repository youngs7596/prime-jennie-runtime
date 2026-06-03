"""STOP 상태에서 발행된 시트의 paper PnL 측정 (v0 — 일봉 simulator).

v3 control STOP 은 fast_loop 스트림 emit 만 차단하고 시트 자체는
`position_sheets` 에 그대로 영속된다 (slow_loop/strategy/publisher.py 의
``publish(sheet, emit_stream=False)``). 이 잡은 평일 18:00 KST (daily_prices
16:00 갱신 + 충분한 마진) 에 도는 cron 으로, 아직 측정 안 된 시트 중 시트의
``time_stop`` hold_days 윈도우가 닫힌 것들을 시뮬레이션해 `paper_outcomes`
테이블에 적재한다.

핵심 설계
---------
- entry: 시트 발행일(`generated_at::date`) 종가. fast_loop 가 시트 받자마자
  진입한다는 모델과 시간 갭이 4-5 시간 있지만 단일 항목으로 단순화.
- exit 평가: ``fast_loop.exit_evaluator.evaluate`` 를 그대로 호출 → 9 룰 충실
  재현, fast_loop 와의 drift 방지.
- v0 은 일봉만 사용해 일중 [open, high, low, close] 4-tick 으로 흘려보낸다.
  ``overextension_exit`` (1분봉 RSI) 는 ``tick.rsi_1m=None`` 이라 evaluator 가
  자동 skip. coverage='daily_only' 로 메타 기록.
- ``scale_out`` 은 발생 시 legs 에 day/price/portion/rule 기록 + 잔여 portion
  은 계속 보유 (state.scale_out_executed 가 갱신되어 같은 level 재발 안 함).
  portion 별 weighted PnL 정밀 처리는 v1 으로 미룸 — exit_price 는 전량 청산
  시점의 가격으로 계산.

v1 (다음 commit) 계획
-------------------
- minute_prices 어댑터 + 1분봉 RSI(14) 계산 → overextension_exit 활성화
- scale_out 부분 청산 weighted exit_price
- corner case 테스트 (death_cross, breakeven 상향 후 SL hit 등)
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from typing import Any

from prime_jennie_runtime.fast_loop.domain import PositionState, TickData
from prime_jennie_runtime.fast_loop.exit_evaluator import evaluate
from prime_jennie_runtime.position_sheet.schema import (
    KST,
    DeathCrossRule,
    PositionSheet,
    TimeStopRule,
)

logger = logging.getLogger(__name__)

SIMULATOR_VERSION = "v0"
ENTRY_MODEL = "close_on_publish_date"
DEFAULT_WINDOW_BDAYS = 20  # time_stop 없는 시트 (validator 가 막지만 안전망)
MA_HISTORY_MARGIN_BDAYS = 5  # death_cross MA long lookback 마진
MAX_BATCH_SHEETS = 500  # 한 cycle 안전망

# 일봉 simulator 의 tick 시각 분포 — KST. 같은 거래일 안에서 [open, high, low,
# close] 4-tick 을 흘려보낸다. close 가 15:30 인 이유는 evaluate 의 time_stop
# cutoff (15:20) 를 그 시점에서만 통과해 hold_days 평가가 마지막 tick 에서만
# 발화하도록 하기 위함.
_TICK_HOURS: tuple[time, ...] = (
    time(9, 5, tzinfo=KST),
    time(10, 30, tzinfo=KST),
    time(14, 0, tzinfo=KST),
    time(15, 30, tzinfo=KST),
)


# =====================================================================
# Entry point
# =====================================================================


async def measure_paper_outcomes(pool: Any, *, today: date | None = None) -> dict:
    """매일 cron 진입점.

    Returns:
        dict 통계 — candidates / measured / data_missing / errors.
    """
    today = today or date.today()
    candidates = await _fetch_pending_sheets(pool, today=today)

    stats = {
        "candidates": len(candidates),
        "measured": 0,
        "data_missing": 0,
        "window_open": 0,
        "errors": 0,
    }

    for row in candidates:
        sheet_id = row["sheet_id"]
        try:
            sheet = PositionSheet.model_validate_json(row["sheet_json"])
        except Exception:
            logger.exception("paper_outcomes: sheet parse failed sheet_id=%s", sheet_id)
            stats["errors"] += 1
            continue

        try:
            outcome = await _simulate_sheet(pool, sheet, today=today)
        except Exception:
            logger.exception("paper_outcomes: simulate failed sheet_id=%s", sheet_id)
            stats["errors"] += 1
            continue

        # 윈도우 미도래 (None) — 영속하지 않고 다음 cycle 재시도. 여기서 행을 박으면
        # _fetch_pending_sheets 의 NOT EXISTS 가 영영 재선택하지 않아, 윈도우가 닫혀도
        # 측정 기회가 사라진다 (P2.5 직후 33건이 이 경로로 data_missing 에 갇혔던 사고).
        if outcome is None:
            stats["window_open"] += 1
            continue

        # upsert 도 시트별로 격리한다 — 한 건의 INSERT 실패(제약 위반 등)가 배치
        # 전체를 롤백해 나머지 멀쩡한 시트까지 못 들어가는 일을 막는다. 도입 초기
        # entry_price NOT NULL 제약이 data_missing 행을 거부하면서 이 격리 부재로
        # 측정 잡이 매일 통째로 죽었다 (migration 023 + 이 격리로 복구).
        try:
            await _upsert_outcome(pool, outcome)
        except Exception:
            logger.exception("paper_outcomes: upsert failed sheet_id=%s", sheet_id)
            stats["errors"] += 1
            continue

        stats["measured"] += 1
        if outcome["exit_reason"] == "data_missing":
            stats["data_missing"] += 1

    logger.info(
        "paper_outcomes: candidates=%d measured=%d data_missing=%d window_open=%d errors=%d",
        stats["candidates"],
        stats["measured"],
        stats["data_missing"],
        stats["window_open"],
        stats["errors"],
    )
    return stats


# =====================================================================
# Sheet selection
# =====================================================================


async def _fetch_pending_sheets(pool: Any, *, today: date) -> list[dict]:
    """측정 미완료 시트 중 ``generated_at::date <= today - 1 day`` 인 것 모음.

    윈도우 닫힘 여부는 ``_simulate_sheet`` 안에서 시트별로 다시 판정 — time_stop
    hold_days 가 시트마다 다르기 때문. 여기선 단순히 어제 이전 발행 + 미측정만
    걸러낸다.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT sheet_id, sheet_json, generated_at
            FROM position_sheets ps
            WHERE sheet_id LIKE 'ps_%'
            AND NOT EXISTS (
                SELECT 1 FROM paper_outcomes po WHERE po.sheet_id = ps.sheet_id
            )
            AND generated_at < $1::date
            ORDER BY generated_at ASC
            LIMIT $2
            """,
            today,
            MAX_BATCH_SHEETS,
        )
    return [dict(r) for r in rows]


# =====================================================================
# Per-sheet simulator
# =====================================================================


async def _simulate_sheet(pool: Any, sheet: PositionSheet, *, today: date) -> dict | None:
    """시트 1건 시뮬레이션.

    Returns:
        outcome dict — 측정 완료 (data_missing 포함, paper_outcomes 에 영속할 대상).
        None — 측정 윈도우가 아직 안 닫힘. 영속하지 않고 다음 cycle 에서 재시도.
    """
    publish_date = sheet.generated_at.astimezone(KST).date()
    ticker = sheet.ticker
    rules_evaluated = [r.type for r in sheet.exit.rules]

    # 1) entry 가격 — 시트 발행일 종가.
    entry_close = await _fetch_daily_close(pool, ticker, publish_date)
    if entry_close is None:
        return _build_outcome(
            sheet=sheet,
            publish_date=publish_date,
            entry_price=None,
            exit_date=None,
            exit_price=None,
            exit_reason="data_missing",
            holding_days=None,
            scale_out_legs=[],
            rules_evaluated=rules_evaluated,
            coverage="daily_only",
            metadata={"reason": "entry_close_missing"},
        )

    # 2) 측정 윈도우 — 시트의 time_stop hold_days value 가 정한다.
    window_bdays = _max_hold_bdays(sheet)
    # publish_date 이후 window_bdays 거래일이 닫혀야 측정 가능.
    expected_window_end = await _add_business_days(pool, publish_date, window_bdays)
    if expected_window_end is None or expected_window_end >= today:
        # daily_prices 가 충분히 안 들어왔거나 윈도우 아직 안 닫힘 — 영속하지 않고
        # 다음 cycle 에서 재시도 (None 반환). data_missing 행을 박으면 영영 재측정 안 됨.
        return None

    # 3) death_cross 가 있으면 entry 직전 MA long + 마진 만큼 일봉 history 필요.
    needs_history = any(isinstance(r, DeathCrossRule) for r in sheet.exit.rules)
    if needs_history:
        max_ma = max(r.ma_long for r in sheet.exit.rules if isinstance(r, DeathCrossRule))
        history_back = max_ma + MA_HISTORY_MARGIN_BDAYS
        history_start = await _subtract_business_days(pool, publish_date, history_back)
    else:
        history_start = publish_date

    # 4) daily_prices fetch — history_start ~ expected_window_end.
    daily_rows = await _fetch_daily_prices(
        pool, ticker, history_start or publish_date, expected_window_end
    )
    sim_days = [r for r in daily_rows if r["price_date"] > publish_date]
    if not sim_days:
        return _build_outcome(
            sheet=sheet,
            publish_date=publish_date,
            entry_price=float(entry_close),
            exit_date=None,
            exit_price=None,
            exit_reason="data_missing",
            holding_days=None,
            scale_out_legs=[],
            rules_evaluated=rules_evaluated,
            coverage="daily_only",
            metadata={"reason": "no_simulation_days"},
        )

    history_closes = [
        float(r["close_price"]) for r in daily_rows if r["price_date"] <= publish_date
    ]

    # 5) state 초기화 — entry_price 기준.
    state = PositionState(
        sheet_id=sheet.sheet_id,
        ticker=ticker,
        entry_price=float(entry_close),
        quantity=1,
        entered_at=sheet.generated_at,
        high_watermark=float(entry_close),
        peak_return_pct=0.0,
    )

    scale_out_legs: list[dict] = []

    # 6) 매 거래일 4-tick 흘려보냄.
    for day_idx, day_row in enumerate(sim_days, start=1):
        day = day_row["price_date"]
        daily_closes = history_closes + [float(r["close_price"]) for r in sim_days[:day_idx]]
        ticks = _build_daily_ticks(day_row, ticker)

        for tick in ticks:
            decision = evaluate(
                sheet,
                state,
                tick,
                now=tick.ts,
                daily_closes=daily_closes if needs_history else None,
                entered_business_days=day_idx,
            )
            if decision is None:
                continue

            if decision.should_close and decision.portion >= 1.0:
                # 전량 청산.
                holding_days = (day - publish_date).days
                return _build_outcome(
                    sheet=sheet,
                    publish_date=publish_date,
                    entry_price=float(entry_close),
                    exit_date=day,
                    exit_price=float(tick.price),
                    exit_reason=decision.matched_rule_type or decision.reason,
                    holding_days=holding_days,
                    scale_out_legs=scale_out_legs,
                    rules_evaluated=rules_evaluated,
                    coverage="daily_only",
                    metadata={
                        "day_idx": day_idx,
                        "decision_metadata": _safe_metadata(decision.metadata),
                        "tick_ts": tick.ts.isoformat(),
                    },
                )

            if decision.should_close and decision.portion < 1.0:
                # scale_out 부분 청산 — leg 기록 + 잔여 보유. v0 은 portion 별
                # weighted PnL 처리 안 함 (다음 commit). state.scale_out_executed 가
                # 같은 level 재발을 막아 무한 loop 없음.
                scale_out_legs.append(
                    {
                        "day": day.isoformat(),
                        "tick_ts": tick.ts.isoformat(),
                        "price": float(tick.price),
                        "portion": decision.portion,
                        "rule": decision.matched_rule_type or decision.reason,
                    }
                )
                continue

            # should_close=False — breakeven SL 상향 같은 경우. evaluate 내부에서
            # state.breakeven_sl_price/breakeven_active 가 이미 갱신됐다.

    # 7) 윈도우 끝 — 마지막 거래일 종가로 강제 close.
    last = sim_days[-1]
    last_close = float(last["close_price"])
    holding_days = (last["price_date"] - publish_date).days
    return _build_outcome(
        sheet=sheet,
        publish_date=publish_date,
        entry_price=float(entry_close),
        exit_date=last["price_date"],
        exit_price=last_close,
        exit_reason="window_expired",
        holding_days=holding_days,
        scale_out_legs=scale_out_legs,
        rules_evaluated=rules_evaluated,
        coverage="daily_only",
        metadata={"day_idx": len(sim_days)},
    )


# =====================================================================
# Tick builders
# =====================================================================


def _build_daily_ticks(day_row: dict, ticker: str) -> list[TickData]:
    """일봉 1개 → [open, high, low, close] 4-tick.

    같은 거래일 안에서 시각 분포는 ``_TICK_HOURS`` 로 KST 09:05/10:30/14:00/15:30.
    마지막 tick (15:30) 이 time_stop cutoff 15:20 를 통과해 hold_days 평가가 그
    시점에서만 발화한다. rsi_1m=None — overextension_exit 룰은 자동 skip.
    """
    day: date = day_row["price_date"]
    base_dt = datetime.combine(day, time(0, 0, tzinfo=KST))
    prices = (
        float(day_row["open_price"]),
        float(day_row["high_price"]),
        float(day_row["low_price"]),
        float(day_row["close_price"]),
    )
    return [
        TickData(
            ticker=ticker,
            price=price,
            ts=base_dt.replace(hour=hour_tz.hour, minute=hour_tz.minute, second=0, microsecond=0),
            rsi_1m=None,
        )
        for price, hour_tz in zip(prices, _TICK_HOURS, strict=True)
    ]


# =====================================================================
# DB helpers
# =====================================================================


async def _fetch_daily_close(pool: Any, ticker: str, day: date) -> float | None:
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT close_price FROM daily_prices WHERE stock_code = $1 AND price_date = $2",
            ticker,
            day,
        )
    return float(val) if val is not None else None


async def _fetch_daily_prices(pool: Any, ticker: str, start: date, end: date) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT price_date, open_price, high_price, low_price, close_price "
            "FROM daily_prices "
            "WHERE stock_code = $1 AND price_date BETWEEN $2 AND $3 "
            "ORDER BY price_date ASC",
            ticker,
            start,
            end,
        )
    return [dict(r) for r in rows]


async def _add_business_days(pool: Any, base: date, bdays: int) -> date | None:
    """daily_prices 의 distinct price_date 를 기준으로 base 다음 N 거래일.

    base 이후 첫 거래일이 1 거래일째. 데이터 없으면 None.
    """
    if bdays <= 0:
        return base
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT price_date FROM daily_prices "
            "WHERE price_date > $1 "
            "GROUP BY price_date ORDER BY price_date ASC OFFSET $2 LIMIT 1",
            base,
            bdays - 1,
        )
    return row["price_date"] if row else None


async def _subtract_business_days(pool: Any, base: date, bdays: int) -> date | None:
    if bdays <= 0:
        return base
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT price_date FROM daily_prices "
            "WHERE price_date < $1 "
            "GROUP BY price_date ORDER BY price_date DESC OFFSET $2 LIMIT 1",
            base,
            bdays - 1,
        )
    return row["price_date"] if row else None


async def _upsert_outcome(pool: Any, outcome: dict) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO paper_outcomes (
                sheet_id, simulator_version, entry_model, coverage,
                entry_date, entry_price, exit_date, exit_price,
                exit_reason, holding_days, pnl_pct,
                scale_out_legs, rules_evaluated, metadata_json, computed_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8,
                $9, $10, $11,
                $12::jsonb, $13::jsonb, $14::jsonb, NOW()
            )
            ON CONFLICT (sheet_id) DO UPDATE SET
                simulator_version = EXCLUDED.simulator_version,
                entry_model       = EXCLUDED.entry_model,
                coverage          = EXCLUDED.coverage,
                entry_date        = EXCLUDED.entry_date,
                entry_price       = EXCLUDED.entry_price,
                exit_date         = EXCLUDED.exit_date,
                exit_price        = EXCLUDED.exit_price,
                exit_reason       = EXCLUDED.exit_reason,
                holding_days      = EXCLUDED.holding_days,
                pnl_pct           = EXCLUDED.pnl_pct,
                scale_out_legs    = EXCLUDED.scale_out_legs,
                rules_evaluated   = EXCLUDED.rules_evaluated,
                metadata_json     = EXCLUDED.metadata_json,
                computed_at       = NOW()
            """,
            outcome["sheet_id"],
            outcome["simulator_version"],
            outcome["entry_model"],
            outcome["coverage"],
            outcome["entry_date"],
            outcome["entry_price"],
            outcome["exit_date"],
            outcome["exit_price"],
            outcome["exit_reason"],
            outcome["holding_days"],
            outcome["pnl_pct"],
            json.dumps(outcome["scale_out_legs"]),
            json.dumps(outcome["rules_evaluated"]),
            json.dumps(outcome["metadata"]),
        )


# =====================================================================
# Helpers
# =====================================================================


def _max_hold_bdays(sheet: PositionSheet) -> int:
    """시트의 time_stop 룰에서 hold_days 최대값. eod 면 1.

    fixed_sl 과 time_stop 은 시트 validator 가 필수로 강제 (schema.py:336-341)
    하므로 time_stop 룰은 항상 존재한다.
    """
    best = 0
    for rule in sheet.exit.rules:
        if not isinstance(rule, TimeStopRule):
            continue
        if rule.mode == "eod":
            best = max(best, 1)
        elif rule.mode == "hold_days" and rule.value is not None:
            best = max(best, int(rule.value))
    return best if best > 0 else DEFAULT_WINDOW_BDAYS


def _safe_metadata(meta: Any) -> dict:
    """ExitDecision.metadata 가 datetime/Decimal 같은 비-JSON 값 가질 때 안전 변환."""
    if not isinstance(meta, dict):
        return {}
    safe: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            safe[k] = v
        else:
            safe[k] = str(v)
    return safe


def _build_outcome(
    *,
    sheet: PositionSheet,
    publish_date: date,
    entry_price: float | None,
    exit_date: date | None,
    exit_price: float | None,
    exit_reason: str,
    holding_days: int | None,
    scale_out_legs: list[dict],
    rules_evaluated: list[str],
    coverage: str,
    metadata: dict,
) -> dict:
    pnl_pct: float | None = None
    if entry_price is not None and exit_price is not None and entry_price > 0:
        pnl_pct = (exit_price - entry_price) / entry_price * 100.0

    return {
        "sheet_id": sheet.sheet_id,
        "simulator_version": SIMULATOR_VERSION,
        "entry_model": ENTRY_MODEL,
        "coverage": coverage,
        "entry_date": publish_date,
        "entry_price": entry_price,
        "exit_date": exit_date,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "holding_days": holding_days,
        "pnl_pct": pnl_pct,
        "scale_out_legs": scale_out_legs,
        "rules_evaluated": rules_evaluated,
        "metadata": metadata,
    }


__all__ = [
    "ENTRY_MODEL",
    "SIMULATOR_VERSION",
    "measure_paper_outcomes",
]
