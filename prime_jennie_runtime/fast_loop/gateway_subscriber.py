"""fast_loop 기동 시 + 주기적으로 kis-gateway 에 실시간 체결·호가 구독을 보증.

v2 원본:
  - prime_jennie/services/monitor/app.py:_subscribe_to_gateway
  - prime_jennie/services/scanner/app.py:_subscribe_to_gateway

v2 에서는 monitor + scanner 가 각자 gateway `/api/realtime/subscribe` 를 호출해
KIS WebSocket 구독을 trigger 했다. v3 에서 이 두 서비스는 fast_loop 로 통합됐지만
subscribe 호출 경로가 포팅되지 않아 gateway streamer 가 dead path 상태였다.
이 모듈이 그 갭을 메운다.

대상 종목 (P2.6, 2026-06-03 교정 → 2026-08-05 빈자리 채움 추가):
  - positions 전체 (현재 보유) — 우선 순위 최상
  - paper 측정 윈도우가 열려있는 시트의 ticker — v2 잔재 watchlist_histories
    (4-17 동결, v3 writer 없음) 를 대체
  - **남는 자리는 직전 거래일 거래대금 상위 KOSPI 종목으로 채운다** (2026-08-05).
    실보유가 0 이고 시트가 서너 종목이라 20종목 중 9개만 쓰고 있었는데, 체결·호가는
    지나가면 되받을 수 없는 데이터라 안 채우면 곧 영구 손실이다.
  - KIS WebSocket 등록 한도 (41) 안에서 자른다.

장중에는 붙어 있는 종목을 바꾸지 않는다 (2026-08-07):
  8-07 오전에 09:30 시트 13건이 발행되면서 구독이 갈렸다. 시트가 채움보다 앞이라
  NAVER·삼성전자우·SK스퀘어·LIG넥스원이 그 시각에 빠졌고, 거래대금 최상위 네 종목의
  그날 나머지가 통째로 사라졌다 — 되받을 수 없는 데이터다. 그래서 우선순위를 뒤집었다.
  주간 스트리머가 붙어 있는 08:50~15:35 에는 **이미 구독된 종목을 그대로 유지**하고,
  새 시트는 남는 여유에만 넣는다. 대상을 다시 세우는 건 그 시간대 밖(사실상 이튿날
  개장 직전)이고, 그때 거래대금 순위도 새 거래일 것으로 갱신된다.

  보유(positions)만은 이 유지 규칙보다 앞이다 — 실제 돈이 들어간 종목의 체결·호가는
  진입·청산 품질 측정의 유일한 재료라 자리를 비켜 주지 않는다.

채움 몫 다섯 고정 (2026-08-07):
  같은 날 저녁에 드러난 두 번째 문제. 측정 대기 시트는 보유일수 창이 닫힐 때까지
  최대 10거래일을 목록에 남는데, 하루 발행량이 4건에서 15건으로 늘자 20종목 중
  19개가 시트가 되고 채움이 SK하이닉스 하나만 남았다 — 거래대금 2위 삼성전자도
  못 들어왔다. 장중에 안 끊기게 고쳐 봐야 애초에 안 붙으면 소용없다. 그래서
  `TURNOVER_RESERVED_CODES` 만큼은 시트가 못 쓰게 떼어 둔다.

구독 보증 (2026-07-08 추가):
  기동 시 1회 호출은 게이트웨이가 아직 안 떠 있으면 실패하고 재시도가 없었다.
  정전 복구 때 fast_loop 이 gateway 보다 22ms 먼저 떠 초기 구독이 유실됐고,
  실시간 체결·호가가 3거래일 끊겼다. 이를 막기 위해 `run_subscription_maintainer`
  가 기동 직후 1회 + 이후 주기적으로 게이트웨이 상태를 확인해, 구독이 죽었거나
  종목이 빠졌으면 재구독한다. 기동 순서 문제도, 중간에 streamer 가 죽는 경우도
  자동 복구된다. tick stream 이 잠시 비어도 price-scheduler 의 5분 REST 폴링이
  가격 fallback 으로 동작한다 (틱·호가 적재는 fallback 없음 — 그래서 보증이 필요).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import asyncpg
import httpx

from prime_jennie_runtime.kis_gateway.market_hours import STREAM_END, STREAM_START
from prime_jennie_runtime.position_sheet.schema import KST

logger = logging.getLogger(__name__)

# KIS WebSocket 한 연결당 등록 가능한 건수 상한.
KIS_WS_SUBSCRIPTION_LIMIT = 41

# 종목마다 체결(H0STCNT0)+호가(H0STASP0) 두 채널을 등록하므로 등록 2건/종목.
# 실효 종목 수는 한도의 절반.
_REGISTRATIONS_PER_CODE = 2
MAX_SUBSCRIPTION_CODES = KIS_WS_SUBSCRIPTION_LIMIT // _REGISTRATIONS_PER_CODE

# 구독 보증 루프 기본 주기 (초). 기동 순서로 초기 구독이 실패해도 이 주기 안에
# 복구된다 — 실시간 유실 최대치를 좌우하므로 짧게 둔다.
SUBSCRIPTION_MAINTAIN_INTERVAL_SEC = 60.0

# 거래대금 상위 채움에 늘 떼어 두는 몫 (2026-08-07 운영자 결정).
# 측정 대기 시트는 보유일수 창이 닫힐 때까지 최대 10거래일을 목록에 남는다. 발행량이
# 하루 4건에서 15건으로 늘자 8-07 저녁엔 20종목 중 19개가 시트가 되고 채움이 한 종목
# (SK하이닉스)까지 줄었다 — 거래대금 2위 삼성전자조차 못 들어왔다. 이 몫이 없으면
# 거래대금 최상위 종목의 연속 시계열이 조용히 0 이 된다.
TURNOVER_RESERVED_CODES = 5

# 측정 윈도우가 열려있는 시트의 ticker — jobs/minute_chart.py 와 같은 기준.
# 최신 시트 우선으로 정렬해 한도 초과 시 오래된 시트부터 떨어져 나가게 한다.
_PENDING_SHEET_TICKERS_SQL = """
    SELECT ps.ticker AS stock_code, MAX(ps.generated_at) AS latest_generated_at
    FROM position_sheets ps
    WHERE ps.sheet_id LIKE 'ps_%'
    AND ps.generated_at >= NOW() - INTERVAL '30 days'
    AND NOT EXISTS (
        SELECT 1 FROM paper_outcomes po WHERE po.sheet_id = ps.sheet_id
    )
    GROUP BY ps.ticker
    ORDER BY latest_generated_at DESC
"""

# 빈자리 채움용 — 직전 거래일 거래대금(종가×거래량) 상위 KOSPI 보통 종목.
# 시스템 정책상 코스닥 제외이고, ETN·ETF 가 섞이지 않게 security_type 도 본다
# (2026-05-25 유니버스 오염 사고와 같은 필터). 종가가 정수형이라 곱하기 전에
# numeric 으로 올린다.
_TOP_TURNOVER_SQL = """
    SELECT d.stock_code
    FROM daily_prices d
    JOIN stock_masters m ON m.stock_code = d.stock_code
    WHERE d.price_date = (SELECT MAX(price_date) FROM daily_prices)
      AND m.is_active
      AND m.market = 'KOSPI'
      AND m.security_type = 'STOCK'
      AND d.close_price IS NOT NULL
      AND d.volume IS NOT NULL
    ORDER BY d.close_price::numeric * d.volume DESC
    LIMIT $1
"""


def _dedup(codes: Sequence[str]) -> list[str]:
    """순서를 지키며 중복 제거."""
    seen: set[str] = set()
    out: list[str] = []
    for code in codes:
        if code not in seen:
            out.append(code)
            seen.add(code)
    return out


def is_stream_window(now: datetime) -> bool:
    """주간 스트리머가 붙어 있는 시간대(08:50~15:35)인지. 이 시간엔 구독을 안 바꾼다.

    휴장일인지는 보지 않는다 — 휴장일엔 어차피 틱이 안 흐르고, 대상을 다시 세우는
    일은 그 시간대 밖에서 매 주기 일어나므로 다음 개장 전에 갱신된다.
    """
    kst = now.astimezone(KST)
    hhmm = kst.hour * 100 + kst.minute
    return STREAM_START <= hhmm < STREAM_END


async def load_subscription_codes(
    pool: asyncpg.Pool,
    *,
    keep: Sequence[str] = (),
) -> list[str]:
    """positions + 유지 종목 + 측정 대기 시트 + 거래대금 상위로 구독 대상을 채운다.

    체결+호가 두 채널이라 실효 종목 한도 (20) 를 넘으면 앞에서부터 자른다. 순서는
    보유 → keep → 측정 대기 시트 최신순 → 거래대금 상위 채움이다.

    keep 은 장중에 이미 구독돼 있는 종목이다 (`ensure_subscribed` 가 넘긴다). 보유
    다음에 두는 이유는 하루 중간에 종목이 갈리면 그 종목의 남은 하루가 통째로
    사라지기 때문이고, 보유보다 뒤에 두는 이유는 실제 돈이 들어간 종목이 먼저여야
    하기 때문이다. 장 시간대 밖에서는 keep 이 비어서 대상이 처음부터 다시 세워진다.

    시트는 `TURNOVER_RESERVED_CODES` 만큼을 남기고 그 앞까지만 쓴다 — 채움 몫이다.
    채움 후보가 모자라면 밀렸던 시트가 그 몫을 도로 가져간다. 구독을 비워 두는 게
    제일 나쁘기 때문이다.
    """
    async with pool.acquire() as conn:
        pos_rows = await conn.fetch("SELECT stock_code FROM positions")
        position_codes = [r["stock_code"] for r in pos_rows]

        sheet_rows = await conn.fetch(_PENDING_SHEET_TICKERS_SQL)
        sheet_codes = [r["stock_code"] for r in sheet_rows]

        held = _dedup(position_codes + list(keep))
        fresh_sheets = [c for c in sheet_codes if c not in set(held)]
        # 채움 몫을 떼고 남는 만큼만 시트가 쓴다. 보유가 많으면 이 예산이 0 이 되고,
        # 그때는 보유가 채움 몫까지 가져간다 — 실 매매 종목이 언제나 먼저다.
        sheet_budget = max(0, MAX_SUBSCRIPTION_CODES - len(held) - TURNOVER_RESERVED_CODES)
        primary = held + fresh_sheets[:sheet_budget]

        if len(primary) < MAX_SUBSCRIPTION_CODES:
            filler_rows = await conn.fetch(_TOP_TURNOVER_SQL, MAX_SUBSCRIPTION_CODES * 2)
            filler_codes = [r["stock_code"] for r in filler_rows]
        else:
            filler_codes = []

    codes: list[str] = []
    seen: set[str] = set()
    for code in primary + filler_codes + fresh_sheets[sheet_budget:]:
        if code in seen:
            continue
        if len(codes) >= MAX_SUBSCRIPTION_CODES:
            break
        codes.append(code)
        seen.add(code)

    # 보유가 밀려나면 진짜 문제다 — 실 매매 종목의 체결·호가를 못 받는다는 뜻이라
    # 한도를 늘리거나 채움을 줄여야 한다.
    if left_out := [c for c in position_codes if c not in seen]:
        logger.warning(
            "보유 종목이 구독 한도 %d 에 밀림 (등록 %d건/종목, KIS 한도 %d): %s",
            MAX_SUBSCRIPTION_CODES,
            _REGISTRATIONS_PER_CODE,
            KIS_WS_SUBSCRIPTION_LIMIT,
            left_out,
        )
    # 시트가 밀리는 건 장중 유지 정책의 정상 결과라 debug 로만 남긴다.
    if sheets_left_out := [c for c in sheet_codes if c not in seen]:
        logger.debug("시트 %d개가 한도에 밀려 미구독: %s", len(sheets_left_out), sheets_left_out)
    return sorted(codes)


async def _post_subscribe(gateway_url: str, codes: list[str], *, timeout: float) -> dict[str, Any]:
    """gateway `/api/realtime/subscribe` 로 구독 요청. 응답 body 반환."""
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{gateway_url}/api/realtime/subscribe",
            json={"codes": codes},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()


async def _post_unsubscribe(
    gateway_url: str, codes: list[str], *, timeout: float
) -> dict[str, Any]:
    """gateway `/api/realtime/unsubscribe` 로 구독 해제. 응답 body 반환."""
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{gateway_url}/api/realtime/unsubscribe",
            json={"codes": codes},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()


async def get_subscription_status(gateway_url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """gateway `/api/realtime/status` 조회. {is_running, subscription_count, codes}."""
    async with httpx.AsyncClient() as http:
        resp = await http.get(f"{gateway_url}/api/realtime/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()


async def subscribe_on_startup(
    pool: asyncpg.Pool,
    gateway_url: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """구독 대상(보유 + 측정 대기 시트 + 거래대금 채움)으로 subscribe 를 1회 요청.

    실패 시 예외를 올리지 않고 warning 로그 후 진행. 반환 dict 는 관측/테스트용.
    상태 확인 없이 곧장 POST 하므로, 재구독 보증은 `ensure_subscribed` 를 쓴다.
    """
    codes = await load_subscription_codes(pool)
    if not codes:
        logger.info("gateway subscribe skipped — 구독 대상 비어있음(일봉까지 없는 상태)")
        return {"codes": [], "skipped": True}

    try:
        body = await _post_subscribe(gateway_url, codes, timeout=timeout)
    except Exception as e:
        logger.warning(
            "gateway subscribe 실패 — tick stream 비활성 상태로 진행 (codes=%d): %s",
            len(codes),
            e,
        )
        return {"codes": codes, "error": str(e)}

    logger.info(
        "gateway subscribe OK — codes=%d added=%d total=%d running=%s",
        len(codes),
        len(body.get("added", [])),
        body.get("total_subscriptions", 0),
        body.get("is_running", False),
    )
    return {"codes": codes, "response": body}


async def ensure_subscribed(
    pool: asyncpg.Pool,
    gateway_url: str,
    *,
    timeout: float = 10.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """구독이 살아있고 원하는 종목을 다 담고 있는지 확인, 아니면 재구독.

    streamer 가 돌고(is_running) 원하는 종목이 모두 구독돼 있으면 아무것도 하지
    않는다(no-op). 그렇지 않으면(기동 순서로 초기 구독 유실, 게이트웨이 재시작 등)
    재구독한다. status 조회 자체가 실패하면(게이트웨이 미준비) 일단 구독을 시도한다.

    **장중(08:50~15:35)에는 이미 붙어 있는 종목을 대상에 그대로 얹는다** — 그래야
    시트가 새로 나와도 붙어 있던 종목이 안 빠진다. 그 시간대 밖에서는 얹지 않으므로
    대상이 처음부터 다시 세워지고, 대상에서 빠진 옛 종목은 아래 해제 경로로 정리된다.

    **구독 요청은 더하기만 한다**(게이트웨이 `/subscribe` 가 add-only). 그래서 놔두면
    등록이 계속 쌓여 KIS 한도(41건)를 넘는다. 한도를 넘길 때만, 그것도 지금 대상이
    아닌 종목만 골라 해제한다 — 한도 안이면 남아 있는 옛 종목도 그냥 둔다(공짜로 더
    받는 데이터라 버릴 이유가 없다).
    """
    try:
        status = await get_subscription_status(gateway_url, timeout=timeout)
    except Exception as e:
        logger.debug("realtime status 조회 실패, 구독 시도로 진행: %s", e)
        status = None

    # 장중이면 붙어 있는 것을 유지 대상으로 넘긴다. 게이트웨이가 안 잡히면 유지할
    # 목록 자체를 모르므로 처음부터 세운다.
    keep: list[str] = []
    if status is not None and is_stream_window(now or datetime.now(KST)):
        keep = list(status.get("codes", []))

    codes = await load_subscription_codes(pool, keep=keep)
    if not codes:
        logger.debug("realtime 구독 보증 skip — 구독 대상 비어있음")
        return {"codes": [], "skipped": True}

    dropped: list[str] = []
    if status is not None:
        running = bool(status.get("is_running"))
        subscribed = set(status.get("codes", []))
        missing = sorted(set(codes) - subscribed)

        # 새로 붙일 것까지 더했을 때 한도를 넘는 만큼만, 대상 밖 종목에서 덜어낸다.
        overflow = len(subscribed) + len(missing) - MAX_SUBSCRIPTION_CODES
        stale = sorted(subscribed - set(codes))
        if overflow > 0 and stale:
            dropped = stale[:overflow]
            try:
                await _post_unsubscribe(gateway_url, dropped, timeout=timeout)
                logger.info("realtime 구독 정리 — 한도 초과 %d개 해제: %s", len(dropped), dropped)
            except Exception as e:
                logger.warning("realtime 구독 해제 실패 (codes=%d): %s", len(dropped), e)
                dropped = []

        if running and not missing:
            return {"codes": codes, "noop": True, "dropped": dropped}
        logger.info(
            "realtime 구독 보증 — running=%s missing=%d → 재구독",
            running,
            len(missing),
        )

    try:
        body = await _post_subscribe(gateway_url, codes, timeout=timeout)
    except Exception as e:
        logger.warning(
            "realtime 재구독 실패 — 다음 주기에 재시도 (codes=%d): %s",
            len(codes),
            e,
        )
        return {"codes": codes, "error": str(e), "dropped": dropped}

    logger.info(
        "realtime 재구독 OK — codes=%d added=%d total=%d running=%s",
        len(codes),
        len(body.get("added", [])),
        body.get("total_subscriptions", 0),
        body.get("is_running", False),
    )
    return {"codes": codes, "response": body, "dropped": dropped}


async def run_subscription_maintainer(
    pool: asyncpg.Pool,
    gateway_url: str,
    *,
    interval_sec: float = SUBSCRIPTION_MAINTAIN_INTERVAL_SEC,
    stop_event: asyncio.Event | None = None,
) -> None:
    """기동 직후 1회 + 이후 주기적으로 실시간 구독을 보증하는 장기 태스크.

    기동 시 게이트웨이가 아직 안 떠 구독이 실패하거나(컨테이너 기동 순서),
    게이트웨이/streamer 가 나중에 죽어 구독이 비면 다음 주기에 자동 복구한다.
    2026-07-08 정전 복구 때 fast_loop 이 gateway 보다 먼저 떠 초기 구독이
    유실되고 재시도 경로가 없어 실시간 체결·호가가 3거래일 끊긴 사고 대응.
    """
    # 기동 직후 1회는 반드시 보증한다(do-while) — 그래서 첫 구독이 stop 이전에
    # 무조건 걸린다. 이후에는 stop_event 를 기다리며 interval 마다 재점검한다.
    while True:
        try:
            await ensure_subscribed(pool, gateway_url)
        except Exception:
            logger.exception("subscription maintainer iteration 실패")

        if stop_event is None:
            await asyncio.sleep(interval_sec)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except TimeoutError:
            pass
        if stop_event.is_set():
            return


__all__ = [
    "is_stream_window",
    "load_subscription_codes",
    "subscribe_on_startup",
    "ensure_subscribed",
    "get_subscription_status",
    "run_subscription_maintainer",
    "KIS_WS_SUBSCRIPTION_LIMIT",
    "MAX_SUBSCRIPTION_CODES",
    "SUBSCRIPTION_MAINTAIN_INTERVAL_SEC",
    "TURNOVER_RESERVED_CODES",
]
