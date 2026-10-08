"""네이버 금융 시장 크롤러 (async 포팅).

v2 `prime_jennie/infra/crawlers/naver_market.py` 의 HTTP 만 `httpx.AsyncClient`
로 변경. 파싱 규칙, fchart 엔드포인트 포맷, 컬럼 인덱스 판정은 유지한다.

- `fetch_index_data(client, index_code)` : 모바일 API 로 KOSPI/KOSDAQ 실시간 지수
- `fetch_investor_flows(client, market, bizdate)` : 외인/기관/개인 순매수 (억원, 웹 JSON API)
- `fetch_market_investor_breakdown(client, bizdate)` : 시장전체 투자자 분해 (연기금 분리)
- `fetch_market_stocks(client, market)` : 시가총액 순위 전종목 (모바일 JSON API)
- `fetch_index_daily_prices(client, index_code, count)` : fchart 일봉 OHLCV
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

import httpx

from .naver_api import NAVER_WEB_API_BASE, get_json, parse_number

logger = logging.getLogger(__name__)

NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


@dataclass
class IndexData:
    close: float
    change_pct: float
    traded_at: date


@dataclass
class InvestorFlows:
    foreign_net: float
    institutional_net: float
    retail_net: float
    trade_date: date


@dataclass
class MarketInvestorBreakdown:
    """시장전체 일별 투자자유형별 순매수 — 연기금이 기관에서 분리된 전체 분해."""

    trade_date: date
    market: str
    individual_net: float  # 개인
    foreign_net: float  # 외국인
    institution_net: float  # 기관계
    financial_inv_net: float  # 금융투자
    insurance_net: float  # 보험
    trust_net: float  # 투신(사모)
    bank_net: float  # 은행
    etc_finance_net: float  # 기타금융기관
    pension_net: float  # 연기금등
    etc_corp_net: float  # 기타법인


@dataclass
class MarketStock:
    stock_code: str
    stock_name: str
    market_cap: int  # 백만원


@dataclass
class IndexDailyOHLCV:
    index_code: str
    price_date: date
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int


async def fetch_index_data(client: httpx.AsyncClient, index_code: str) -> IndexData | None:
    url = f"https://m.stock.naver.com/api/index/{index_code}/basic"
    try:
        resp = await client.get(url, headers=NAVER_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        close = float(data["closePrice"].replace(",", ""))
        change_pct = float(data["fluctuationsRatio"])
        traded_at_str = data["localTradedAt"][:10]
        return IndexData(
            close=close,
            change_pct=change_pct,
            traded_at=date.fromisoformat(traded_at_str),
        )
    except Exception as e:
        logger.warning("Naver index fetch failed (%s): %s", index_code, e)
        return None


async def fetch_investor_flows(
    client: httpx.AsyncClient, market: str, bizdate: str
) -> InvestorFlows | None:
    """외인/기관/개인 순매수 (억원). 시장 전체 일별 수급에서 ``bizdate`` 한 줄을 고른다.

    출처가 KOSPI 시장 전체만 다뤄서 ``market`` 은 받기만 하고 쓰지 않는다
    (council_macro 호출부 호환). 장중에 부르면 그날 줄은 장중 누적값이다."""
    rows = await fetch_market_investor_breakdown(client, bizdate)
    for r in rows:
        if r.trade_date.strftime("%Y%m%d") == bizdate:
            return InvestorFlows(
                foreign_net=r.foreign_net,
                institutional_net=r.institution_net,
                retail_net=r.individual_net,
                trade_date=r.trade_date,
            )
    logger.warning("Naver investor: no row for date %s (%s)", bizdate, market)
    return None


# 시장 전체 투자자 수급 — 2026-09-16 무렵 옛 `investorDealTrendDay` HTML 이 410 으로
# 닫혀 새 웹 화면이 읽는 JSON 으로 옮겼다. 응답은 거래일마다 투자자 코드별 순매수
# (원 단위 문자열)를 준다.
_TREND_DAILY_PATH = "/domestic/market/trend/daily"
_TREND_PAGE_SIZE = 20

# 투자자 코드 → MarketInvestorBreakdown 필드. 2026-09-15 운영 DB 의 옛 화면 값과
# 코드별로 대조해 정했다 (전부 억원 단위로 일치). 옛 화면이 합쳐 보이던 것은 여기서도
# 합친다: 외국인 = 외국인 + 기타외국인, 투신(사모) = 투신 + 사모, 연기금등 = 연기금 +
# 국가·지자체 (국가·지자체는 20 거래일 내내 0 이었다).
_INVESTOR_CODE_FIELDS: dict[str, str] = {
    "8000": "individual_net",  # 개인
    "9000": "foreign_net",  # 외국인
    "9001": "foreign_net",  # 기타외국인
    "1000": "financial_inv_net",  # 금융투자
    "2000": "insurance_net",  # 보험
    "3000": "trust_net",  # 투신
    "3100": "trust_net",  # 사모
    "4000": "bank_net",  # 은행
    "5000": "etc_finance_net",  # 기타금융
    "6000": "pension_net",  # 연기금
    "7000": "pension_net",  # 국가·지자체
    "7100": "etc_corp_net",  # 기타법인
}

# 기관계 = 기관 하위 여섯 항목의 합. 새 응답에는 기관계 합계 줄이 따로 없다.
_INSTITUTION_FIELDS = (
    "financial_inv_net",
    "insurance_net",
    "trust_net",
    "bank_net",
    "etc_finance_net",
    "pension_net",
)

_WON_PER_EOK = 100_000_000


def _parse_trend_row(row: dict) -> MarketInvestorBreakdown | None:
    """응답 한 거래일 줄을 억원 단위 분해로. 날짜가 이상하면 None."""
    bizdate = str(row.get("bizdate") or "")
    if len(bizdate) != 8 or not bizdate.isdigit():
        return None
    won: dict[str, int] = {f: 0 for f in set(_INVESTOR_CODE_FIELDS.values())}
    for amt in row.get("netAmounts") or []:
        code = str(amt.get("investorGubun") or "")
        value = parse_number(amt.get("diffValue"))
        if value is None:
            continue
        field = _INVESTOR_CODE_FIELDS.get(code)
        if field is None:
            # 모르는 코드가 생기면 개인+외국인+기관+기타법인 = 0 항등식이 깨져 계약
            # 검사가 잡는다. 여기서는 흔적만 남긴다.
            if value != 0:
                logger.warning("Naver investor: unknown code %s (%s)", code, bizdate)
            continue
        won[field] += int(value)
    # 원 단위로 다 더한 뒤 억원으로 반올림 — 옛 화면도 억원 정수로 보여 줬다.
    fields = {f: float(round(v / _WON_PER_EOK)) for f, v in won.items()}
    fields["institution_net"] = float(
        round(sum(won[f] for f in _INSTITUTION_FIELDS) / _WON_PER_EOK)
    )
    trade_date = date(int(bizdate[0:4]), int(bizdate[4:6]), int(bizdate[6:8]))
    return MarketInvestorBreakdown(trade_date=trade_date, market="KOSPI", **fields)


async def fetch_market_investor_breakdown(
    client: httpx.AsyncClient, bizdate: str
) -> list[MarketInvestorBreakdown]:
    """KOSPI 시장전체 일별 투자자유형별 순매수(연기금 분리). ``bizdate`` 부터 거슬러
    최근 20 거래일을 최신순으로 돌려준다(수집기가 일괄 upsert → 공백 self-heal).
    단위는 억원(1e8 KRW), 부호=순매수. 실패하면 빈 리스트.

    코스닥 수급이 필요해지면 ``marketType`` 만 바꿔 보면 되지만, 지금은 KOSPI 만
    쓰므로 고정한다."""
    data = await get_json(
        client,
        _TREND_DAILY_PATH,
        params={
            "tradeType": "KRX",
            "marketType": "KOSPI",
            "bizdate": bizdate,
            "startIdx": "0",
            "pageSize": str(_TREND_PAGE_SIZE),
        },
        base=NAVER_WEB_API_BASE,
    )
    content = data.get("content") if isinstance(data, dict) else None
    if not content:
        logger.warning("Naver investor breakdown: no data rows (%s)", bizdate)
        return []
    results = [r for r in (_parse_trend_row(row) for row in content) if r is not None]
    if not results:
        logger.warning("Naver investor breakdown: no parsable rows (%s)", bizdate)
    return results


_MARKET_VALUE_PAGE_SIZE = 100


async def fetch_market_stocks(
    client: httpx.AsyncClient, market: str = "KOSPI", *, request_delay: float = 0.15
) -> list[MarketStock]:
    """시가총액 순위 전종목 (시총 단위는 백만원).

    2026-09-15 에 옛 시가총액 HTML 페이지에서 모바일 JSON API 로 옮겼다. 순서
    (시총 내림차순)와 담기는 종목 범위는 그대로다.
    """
    category = "KOSPI" if market.upper() == "KOSPI" else "KOSDAQ"
    stocks: list[MarketStock] = []
    seen: set[str] = set()
    page = 1

    while True:
        data = await get_json(
            client,
            f"/stocks/marketValue/{category}",
            params={"page": page, "pageSize": _MARKET_VALUE_PAGE_SIZE},
        )
        if not isinstance(data, dict):
            break

        rows = data.get("stocks") or []
        for row in rows:
            code = str(row.get("itemCode") or "")
            name = str(row.get("stockName") or "").strip()
            if len(code) != 6 or not code.isdigit() or code in seen or not name:
                continue
            cap_mil = _market_cap_million(row)
            if cap_mil is None:
                continue
            seen.add(code)
            stocks.append(MarketStock(stock_code=code, stock_name=name, market_cap=cap_mil))

        total = data.get("totalCount")
        if len(rows) < _MARKET_VALUE_PAGE_SIZE:
            break
        if isinstance(total, int) and page * _MARKET_VALUE_PAGE_SIZE >= total:
            break
        page += 1
        await asyncio.sleep(request_delay)

    logger.info("Naver market stocks (%s): %d", market, len(stocks))
    return stocks


def _market_cap_million(row: dict) -> int | None:
    """시총을 백만원으로. 원 단위 raw 값을 먼저 쓰고 없으면 억원 표시값을 환산."""
    raw = parse_number(row.get("marketValueRaw"))
    if raw is not None and raw > 0:
        return int(raw // 1_000_000)
    eok = parse_number(row.get("marketValue"))
    if eok is not None and eok > 0:
        return int(eok * 100)
    return None


_FCHART_INDEX_CODE = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ"}


async def fetch_index_daily_prices(
    client: httpx.AsyncClient, index_code: str, count: int = 250
) -> list[IndexDailyOHLCV]:
    """fchart 에서 지수 일봉 OHLCV. 오래된 순 정렬."""
    fchart_code = _FCHART_INDEX_CODE.get(index_code.upper(), index_code.upper())
    url = "https://fchart.stock.naver.com/sise.nhn"
    try:
        resp = await client.get(
            url,
            headers=NAVER_HEADERS,
            params={
                "symbol": fchart_code,
                "timeframe": "day",
                "count": str(count),
                "requestType": "0",
            },
            timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        items: list[IndexDailyOHLCV] = []
        for item in root.iter("item"):
            data = item.get("data", "")
            parts = data.split("|")
            if len(parts) < 6:
                continue
            try:
                price_date = date(int(parts[0][:4]), int(parts[0][4:6]), int(parts[0][6:8]))
                items.append(
                    IndexDailyOHLCV(
                        index_code=index_code.upper(),
                        price_date=price_date,
                        open_price=float(parts[1]),
                        high_price=float(parts[2]),
                        low_price=float(parts[3]),
                        close_price=float(parts[4]),
                        volume=int(parts[5]),
                    )
                )
            except (ValueError, IndexError) as e:
                logger.debug("fchart item parse skip: %s — %s", data, e)
                continue
        items.sort(key=lambda x: x.price_date)
        logger.info("fchart %s: %d bars", index_code, len(items))
        return items
    except Exception as e:
        logger.warning("fchart index fetch failed (%s): %s", index_code, e)
        return []
