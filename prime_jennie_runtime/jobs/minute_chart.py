"""5분봉 자동 수집 (시총 상위 30 + 측정 대기 시트) — paper 측정용 수집기.

v2 원본: `/jobs/collect-minute-chart` (services/jobs/app.py:624-699).

v3 어댑터:
- v2 KIS 직접 호출 → kis_gateway HTTP `/api/market/minute-prices`.
- v2 in-line INSERT 검사 → PostgresPriceRepo.upsert_minute (executemany 일괄).
- 2026-05-08 부터 KIS 분봉 단일 수집자. price_scheduler.collect_minute 은 5종목
  sample placeholder 였던 잔재 잡으로 obsolete (top30 안에 모두 포함되어 100% 중복).

수집 대상 (P2.6, 2026-06-03 교정):
- stock_masters 시총 상위 N (백테스트 참고용 기본 커버리지)
- paper 측정 윈도우가 아직 열려있는 시트의 ticker — v2 잔재 watchlist_histories
  (4-17 동결, v3 writer 없음) 를 대체. 측정하려는 시트의 분봉이 정작 안 쌓이던
  결함을 고침 (P3 1분봉 simulator 의 전제).

페이싱 (2026-06-12 적응형 재설계 — "분봉 버스트 펴기"):
- 이전엔 고정 10 req/s 로 몰아쳐 45종목이면 ~5초간 게이트웨이 전역 예산(9/s)을
  독식했고, 같은 1초 예산을 쓰는 잔고 폴이 KIS EGW00201 폭풍에 휘말렸다
  (6-12 13:45 monitor 거짓 critical). KIS 거부는 순간 버스트가 아니라 **1초
  누적 평균**이 좌우하므로 (6-5 실측 PoC), 손잡이는 동시성이 아니라 평균
  속도다 — 종목 수에서 간격을 연산해 수집을 창(기본 240초) 전체에 편다.
- 적응: 거부(5xx/429)·시간초과가 보이면 간격을 배로 늘리고 그 종목은 말미에
  한 번 재시도, 성공이 이어지면 간격을 기본으로 점진 회복.
- 대상이 아주 많아 간격 하한(0.35초 ≈ 2.9 req/s)에 닿으면 창을 넘길 수 있다 —
  경고 로그만 남기고 하한을 지킨다 (KIS 안전 우선. cron 은 max_instances=1
  이라 다음 주기와 겹치지 않고 skip 된다). 분봉은 과거 봉 조회라 창 안에서
  늦게 수집될수록 오히려 완결된 봉을 받는다 — 신선도 손해 없음.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from prime_jennie_runtime.kis_gateway.price_repo import PostgresPriceRepo
from prime_jennie_runtime.kis_gateway.schemas import MinutePrice

logger = logging.getLogger(__name__)


# 수집을 펼칠 창 — 5분 cron 주기에서 60초 여유.
_WINDOW_SEC = 240.0
# 간격 하한 (≈ 2.9 req/s) — 게이트웨이 전역 9/s 의 1/3 만 쓰고 나머지는
# 잔고 폴·시세·주문에 양보.
_MIN_INTERVAL_SEC = 0.35
# 혼잡 신호 시 간격 증가 배수 / 상한 (base 의 8배, 절대 30초).
_BACKOFF_FACTOR = 2.0
_BACKOFF_MAX_MULT = 8.0
_BACKOFF_ABS_CAP_SEC = 30.0
# 성공 시 간격을 base 쪽으로 되돌리는 비율.
_RECOVERY_FACTOR = 0.8

# 측정 대기 시트 ticker 조회 — paper_outcomes 의 _fetch_pending_sheets 와 같은 기준.
# 30일 안전 상한: 측정이 다시 멈추는 사고가 나도 수집 대상이 무한정 자라
# KIS 호출량이 폭증하는 일을 막는다 (윈도우 최대 10 거래일 ≈ 14 달력일 + 여유).
PENDING_SHEET_TICKERS_SQL = """
    SELECT DISTINCT ps.ticker AS stock_code
    FROM position_sheets ps
    WHERE ps.sheet_id LIKE 'ps_%'
    AND ps.generated_at >= NOW() - INTERVAL '30 days'
    AND NOT EXISTS (
        SELECT 1 FROM paper_outcomes po WHERE po.sheet_id = ps.sheet_id
    )
"""


def compute_base_interval(
    n_targets: int,
    *,
    window_sec: float = _WINDOW_SEC,
    min_interval_sec: float = _MIN_INTERVAL_SEC,
) -> float:
    """종목 수에서 요청 간격을 연산 — 창에 고르게 펴되 하한 아래로는 안 내려간다.

    예: 45종목 → 240/45 = 5.33초 간격 (0.19 req/s). 100종목 → 2.4초.
    686종목부터 하한 0.35초에 닿아 창(240초)을 넘기기 시작 — 호출부가 경고.
    """
    if n_targets <= 0:
        return 0.0
    return max(window_sec / n_targets, min_interval_sec)


def _is_throttle_error(e: Exception) -> bool:
    """감속 + 말미 재시도 가치가 있는 실패 — KIS/게이트웨이 혼잡 신호.

    5xx (gateway 가 KIS EGW00201 을 500 으로 중계) / 429 / 전송 오류(시간초과,
    연결 실패). 4xx 등 그 외는 혼잡이 아니라 요청 문제 — 재시도 안 함.
    """
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        return status == 429 or status >= 500
    return isinstance(e, httpx.RequestError)


async def collect_minute_chart(
    pool: Any,
    http: httpx.AsyncClient,
    gateway_url: str,
    *,
    top_n: int = 30,
    window_sec: float = _WINDOW_SEC,
    min_interval_sec: float = _MIN_INTERVAL_SEC,
) -> dict:
    """v2 `/jobs/collect-minute-chart` 포팅 + P2.6 수집 대상 교정 + 적응형 페이싱.

    1) stock_masters 활성 + 시총 desc 상위 N (default 30)
    2) 측정 윈도우가 열려있는 시트 (paper_outcomes 미적재) 의 ticker 추가
    3) 종목 수로 간격을 연산해 창에 고르게 분산, 혼잡 신호 시 감속 + 말미 1회
       재시도 (gateway 전역 limiter 가 최종 방어선)
    4) 각 종목 gateway `/api/market/minute-prices` POST → minute_prices upsert

    Returns: {"target", "top_n", "pending_sheets_added", "upserted", "failed",
              "interval_sec", "duration_sec", "backoffs"}
    """
    # 대상 조회는 connection 을 짧게 쓰고 바로 반환 — 긴 페이싱 루프 동안
    # pool connection 을 점유하면 같은 worker 의 다른 잡이 굶는다.
    async with pool.acquire() as conn:
        top_rows = await conn.fetch(
            "SELECT stock_code FROM stock_masters "
            "WHERE is_active = TRUE AND market_cap IS NOT NULL "
            "ORDER BY market_cap DESC LIMIT $1",
            top_n,
        )
        target_codes = {r["stock_code"] for r in top_rows}
        top_count = len(target_codes)

        sheet_rows = await conn.fetch(PENDING_SHEET_TICKERS_SQL)
        pending_sheets_added = 0
        for r in sheet_rows:
            if r["stock_code"] not in target_codes:
                target_codes.add(r["stock_code"])
                pending_sheets_added += 1

    target_list = sorted(target_codes)
    n = len(target_list)
    base_interval = compute_base_interval(
        n, window_sec=window_sec, min_interval_sec=min_interval_sec
    )
    estimated = base_interval * n
    logger.info(
        "collect_minute_chart: targets=%d (top%d=%d + pending_sheets=%d) "
        "interval=%.2fs estimated=%.0fs",
        n,
        top_n,
        top_count,
        pending_sheets_added,
        base_interval,
        estimated,
    )
    if n and estimated > window_sec * 1.25:
        logger.warning(
            "collect_minute_chart: 대상 %d 이 많아 추정 소요 %.0fs 가 창 %.0fs 를 "
            "초과 — 간격 하한(%.2fs) 유지 (KIS 안전 우선, 다음 주기는 skip 될 수 있음)",
            n,
            estimated,
            window_sec,
            min_interval_sec,
        )

    # repo 는 pool 직접 사용 (executemany 가 내부에서 짧게 acquire).
    repo = PostgresPriceRepo(conn=pool)
    upserted = 0
    failed = 0
    backoffs = 0
    interval = base_interval
    retried: set[str] = set()
    queue = list(target_list)
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_request = 0.0

    while queue:
        code = queue.pop(0)
        wait = last_request + interval - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        last_request = loop.time()
        try:
            resp = await http.post(
                f"{gateway_url}/api/market/minute-prices",
                json={"stock_code": code},
                timeout=30.0,
            )
            resp.raise_for_status()
            items = [MinutePrice.model_validate(x) for x in resp.json()]
            upserted += await repo.upsert_minute(items)
            # 혼잡으로 늘렸던 간격을 기본 쪽으로 점진 회복.
            interval = max(base_interval, interval * _RECOVERY_FACTOR)
        except Exception as e:
            if _is_throttle_error(e) and code not in retried:
                retried.add(code)
                queue.append(code)
                interval = min(
                    max(interval, min_interval_sec) * _BACKOFF_FACTOR,
                    max(base_interval, min_interval_sec) * _BACKOFF_MAX_MULT,
                    _BACKOFF_ABS_CAP_SEC,
                )
                backoffs += 1
                logger.warning(
                    "collect_minute_chart throttle %s — 간격 %.2fs 로 감속, 말미 재시도: %s",
                    code,
                    interval,
                    e,
                )
            else:
                failed += 1
                logger.warning("collect_minute_chart failed %s: %s", code, e)

    duration = loop.time() - started
    logger.info(
        "collect_minute_chart: target=%d upserted=%d failed=%d backoffs=%d duration=%.1fs",
        n,
        upserted,
        failed,
        backoffs,
        duration,
    )
    return {
        "target": n,
        "top_n": top_count,
        "pending_sheets_added": pending_sheets_added,
        "upserted": upserted,
        "failed": failed,
        "interval_sec": round(base_interval, 2),
        "duration_sec": round(duration, 1),
        "backoffs": backoffs,
    }


__all__ = ["PENDING_SHEET_TICKERS_SQL", "collect_minute_chart", "compute_base_interval"]
