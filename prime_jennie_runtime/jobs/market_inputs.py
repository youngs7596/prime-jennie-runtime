"""매크로 게이트 입력 수집 — VKOSPI 일별 + 시장전체 투자자 수급(연기금 분리).

2026-06-24 도입. 둘 다 비공식 출처(CNBC 내부 API, 네이버 페이지)라 값 sanity 가드를
두고, idempotent upsert 로 공백을 self-heal 한다. 한 번의 호출이 최근 여러 거래일을
덮으므로(네이버 페이지·CNBC range) 하루 한 번 cron 이면 충분하다.

- collect_vkospi: CNBC .KSVKOSPI → vkospi_daily.
- collect_market_investor_flows: 네이버 investorDealTrendDay → market_investor_flows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .crawlers.cnbc_market import fetch_vkospi_daily
from .crawlers.naver_market import fetch_market_investor_breakdown

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# VKOSPI 정상 범위 가드 — 비공식 API 스키마 변경·이상치 방어. 역사적으로 대략 10~90,
# 극단 폭락기 100 근처. 1~300 밖이면 파싱 오류로 보고 버린다.
_VKOSPI_MIN, _VKOSPI_MAX = 1.0, 300.0


async def collect_vkospi(
    pool: Any, http: httpx.AsyncClient, *, range_token: str = "1M"
) -> dict[str, int]:
    """CNBC 에서 VKOSPI 일별을 받아 vkospi_daily 에 upsert. range_token 기본 1M(≈1년 일봉,
    증분·공백 self-heal 충분). 최초 백필은 '6M'(≈3년 일봉, 가장 깊음) — 5Y/ALL 은 주봉/월봉
    이라 일봉 백필엔 쓰지 말 것(토큰 이름이 깊이·주기와 안 맞음, cnbc_market 참조)."""
    bars = await fetch_vkospi_daily(http, range_token)
    valid = [b for b in bars if _VKOSPI_MIN <= b.close_price <= _VKOSPI_MAX]
    dropped = len(bars) - len(valid)
    upserted = 0
    async with pool.acquire() as conn:
        for b in valid:
            await conn.execute(
                "INSERT INTO vkospi_daily "
                "(price_date, open_price, high_price, low_price, close_price, source, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,'cnbc',NOW()) "
                "ON CONFLICT (price_date) DO UPDATE SET "
                "open_price=EXCLUDED.open_price, high_price=EXCLUDED.high_price, "
                "low_price=EXCLUDED.low_price, close_price=EXCLUDED.close_price, "
                "updated_at=NOW()",
                b.price_date,
                b.open_price,
                b.high_price,
                b.low_price,
                b.close_price,
            )
            upserted += 1
    logger.info("collect_vkospi: upserted=%d dropped=%d (range=%s)", upserted, dropped, range_token)
    return {"upserted": upserted, "dropped": dropped}


async def collect_market_investor_flows(
    pool: Any, http: httpx.AsyncClient, *, markets: tuple[str, ...] = ("kospi", "kosdaq")
) -> dict[str, int]:
    """네이버 시장전체 투자자유형별 순매수(연기금 분리)를 market_investor_flows 에 upsert.
    한 페이지가 최근 ~20거래일을 담으므로 한 번 호출로 최근 윈도우를 통째로 적재한다."""
    bizdate = datetime.now(KST).strftime("%Y%m%d")
    upserted = 0
    async with pool.acquire() as conn:
        for market in markets:
            rows = await fetch_market_investor_breakdown(http, market, bizdate)
            for r in rows:
                await conn.execute(
                    "INSERT INTO market_investor_flows "
                    "(trade_date, market, individual_net, foreign_net, institution_net, "
                    " financial_inv_net, insurance_net, trust_net, bank_net, etc_finance_net, "
                    " pension_net, etc_corp_net, unit, source, updated_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'million_krw','naver',NOW()) "
                    "ON CONFLICT (trade_date, market) DO UPDATE SET "
                    "individual_net=EXCLUDED.individual_net, foreign_net=EXCLUDED.foreign_net, "
                    "institution_net=EXCLUDED.institution_net, "
                    "financial_inv_net=EXCLUDED.financial_inv_net, "
                    "insurance_net=EXCLUDED.insurance_net, trust_net=EXCLUDED.trust_net, "
                    "bank_net=EXCLUDED.bank_net, etc_finance_net=EXCLUDED.etc_finance_net, "
                    "pension_net=EXCLUDED.pension_net, etc_corp_net=EXCLUDED.etc_corp_net, "
                    "updated_at=NOW()",
                    r.trade_date,
                    r.market,
                    r.individual_net,
                    r.foreign_net,
                    r.institution_net,
                    r.financial_inv_net,
                    r.insurance_net,
                    r.trust_net,
                    r.bank_net,
                    r.etc_finance_net,
                    r.pension_net,
                    r.etc_corp_net,
                )
                upserted += 1
    logger.info("collect_market_investor_flows: upserted=%d", upserted)
    return {"upserted": upserted}


__all__ = ["collect_market_investor_flows", "collect_vkospi"]
