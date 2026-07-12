"""매크로 게이트 입력 수집 — VKOSPI 일별 + 시장전체 투자자 수급(연기금 분리).

2026-06-24 도입. 둘 다 비공식 출처(CNBC 내부 API, 네이버 페이지)라 값 sanity 가드를
두고, idempotent upsert 로 공백을 self-heal 한다. 한 번의 호출이 최근 여러 거래일을
덮으므로(네이버 페이지·CNBC range) 하루 한 번 cron 이면 충분하다.

- collect_vkospi: CNBC .KSVKOSPI → vkospi_daily.
- collect_market_investor_flows: 네이버 investorDealTrendDay → market_investor_flows.
- collect_futures_oi: KIS 선물 시세(gateway) → futures_oi_snapshots. 2026-07-12 추가.
  이쪽만 출처가 공식(KIS)이라 sanity 가드는 얇지만, 과거치를 안 줘서 백필이 불가능하다.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
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


async def collect_market_investor_flows(pool: Any, http: httpx.AsyncClient) -> dict[str, int]:
    """네이버 시장전체(KOSPI) 투자자유형별 순매수(연기금 분리)를 market_investor_flows 에
    upsert. 출처가 KOSPI 시장전체만 줘서(sosession 무시, 2026-06-24 실측) KOSPI 한 시장만
    적재한다. 한 페이지가 최근 ~20거래일을 담으므로 한 번 호출로 최근 윈도우를 통째로 적재."""
    bizdate = datetime.now(KST).strftime("%Y%m%d")
    rows = await fetch_market_investor_breakdown(http, bizdate)
    upserted = 0
    async with pool.acquire() as conn:
        for r in rows:
            await conn.execute(
                "INSERT INTO market_investor_flows "
                "(trade_date, market, individual_net, foreign_net, institution_net, "
                " financial_inv_net, insurance_net, trust_net, bank_net, etc_finance_net, "
                " pension_net, etc_corp_net, unit, source, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'eok_krw','naver',NOW()) "
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


_FUTURES_SLOTS = ("preopen", "close", "night_open", "night_close")

# 미결제약정 sanity 가드 — 코스피200 선물 OI 는 역사적으로 10만~40만 계약대(근월물).
# 차월물은 수천 계약대라 하한은 낮게 둔다. 0 이나 비현실적 값이면 응답 이상으로 보고 버린다.
_OI_MIN, _OI_MAX = 1, 5_000_000


def _parse_date(val: Any) -> date | None:
    """gateway JSON 의 ISO 날짜 문자열 → date (asyncpg 는 date 객체를 요구)."""
    if not val:
        return None
    try:
        return date.fromisoformat(str(val))
    except ValueError:
        return None


async def collect_futures_oi(
    pool: Any, http: httpx.AsyncClient, kis_gateway_url: str, *, slot: str
) -> dict[str, Any]:
    """KOSPI200 선물 근월·차월물의 미결제약정·베이시스를 futures_oi_snapshots 에 적재.

    하루 4슬롯(preopen/close/night_open/night_close). night_close 는 익일 05:05 에 돌아
    전일 야간장을 관측하므로 trade_date 를 하루 당겨 적재한다 — 같은 trade_date 안에서
    close → night_close OI 차이가 '야간 청산분'이 된다.

    **근월물만 찍지 않는다.** 만기 주간엔 OI 가 차월물로 이전되므로 근월물 델타만 보면
    롤오버가 청산으로 오독된다(민지 리뷰 2026-07-12). 두 계약을 모두 행으로 남기고
    is_front 로 근월물을 표시 → 분석은 합산 OI 로 롤오버를 중화하고, 필요하면 계약별
    이전량도 볼 수 있다.

    휴장일엔 행을 아예 안 남긴다(전일값 복사 금지). preopen/close/night_open 은 호출부의
    거래일 가드가, night_close 는 아래 DB 가드가 막는다 — 그 trade_date 에 close 스냅샷이
    없으면 애초에 거래일이 아니었으므로 야간장도 없다. (05:05 시점의 '오늘' 거래일 판정은
    금요일 밤 세션이 토요일 새벽에 끝나는 구조와 어긋나 못 쓴다.)
    """
    if slot not in _FUTURES_SLOTS:
        raise ValueError(f"unknown slot: {slot} (allowed: {_FUTURES_SLOTS})")

    now = datetime.now(KST)
    trade_date = (now - timedelta(days=1)).date() if slot == "night_close" else now.date()

    if slot == "night_close":
        async with pool.acquire() as conn:
            has_close = await conn.fetchval(
                "SELECT 1 FROM futures_oi_snapshots WHERE trade_date=$1 AND slot='close' LIMIT 1",
                trade_date,
            )
        if not has_close:
            logger.info("collect_futures_oi[night_close] skipped: %s 는 거래일 아님", trade_date)
            return {"skipped": True, "trade_date": str(trade_date)}

    resp = await http.get(f"{kis_gateway_url}/api/futures/kospi200", timeout=20.0)
    resp.raise_for_status()
    quotes = resp.json()

    inserted, dropped = 0, 0
    async with pool.acquire() as conn:
        for q in quotes:
            oi = int(q["open_interest"])
            if not _OI_MIN <= oi <= _OI_MAX:
                logger.warning(
                    "collect_futures_oi[%s] %s dropped: OI=%d 범위 밖",
                    slot,
                    q.get("contract_code"),
                    oi,
                )
                dropped += 1
                continue
            await conn.execute(
                "INSERT INTO futures_oi_snapshots "
                "(trade_date, slot, contract_code, contract_name, is_front, captured_at, "
                " futures_price, open_interest, oi_change, volume, basis, market_basis, "
                " theoretical_price, disparity, kospi_index, remaining_days, "
                " last_trade_date, source) "
                "VALUES ($1,$2,$3,$4,$5,NOW(),$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,'kis') "
                "ON CONFLICT (trade_date, slot, contract_code) DO UPDATE SET "
                "contract_name=EXCLUDED.contract_name, is_front=EXCLUDED.is_front, "
                "captured_at=NOW(), futures_price=EXCLUDED.futures_price, "
                "open_interest=EXCLUDED.open_interest, oi_change=EXCLUDED.oi_change, "
                "volume=EXCLUDED.volume, basis=EXCLUDED.basis, "
                "market_basis=EXCLUDED.market_basis, "
                "theoretical_price=EXCLUDED.theoretical_price, disparity=EXCLUDED.disparity, "
                "kospi_index=EXCLUDED.kospi_index, remaining_days=EXCLUDED.remaining_days, "
                "last_trade_date=EXCLUDED.last_trade_date",
                trade_date,
                slot,
                q["contract_code"],
                q.get("contract_name"),
                bool(q.get("is_front")),
                q.get("price"),
                oi,
                int(q.get("oi_change") or 0),
                int(q.get("volume") or 0),
                q.get("basis"),
                q.get("market_basis"),
                q.get("theoretical_price"),
                q.get("disparity"),
                q.get("kospi_index"),
                q.get("remaining_days"),
                _parse_date(q.get("last_trade_date")),
            )
            inserted += 1

    front = next((q for q in quotes if q.get("is_front")), None)
    total_oi = sum(int(q["open_interest"]) for q in quotes)
    logger.info(
        "collect_futures_oi[%s] %s 계약%d건 합산OI=%d 근월=%s OI=%s basis=%s",
        slot,
        trade_date,
        inserted,
        total_oi,
        front and front.get("contract_code"),
        front and front.get("open_interest"),
        front and front.get("basis"),
    )
    return {
        "trade_date": str(trade_date),
        "slot": slot,
        "contracts": inserted,
        "dropped": dropped,
        "total_open_interest": total_oi,
    }


__all__ = ["collect_futures_oi", "collect_market_investor_flows", "collect_vkospi"]
