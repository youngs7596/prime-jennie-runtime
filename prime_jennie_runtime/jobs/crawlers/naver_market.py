"""네이버 금융 시장 크롤러 (async 포팅).

v2 `prime_jennie/infra/crawlers/naver_market.py` 의 HTTP 만 `httpx.AsyncClient`
로 변경. 파싱 규칙, fchart 엔드포인트 포맷, 컬럼 인덱스 판정은 유지한다.

- `fetch_index_data(client, index_code)` : 모바일 API 로 KOSPI/KOSDAQ 실시간 지수
- `fetch_investor_flows(client, market, bizdate)` : 외인/기관/개인 순매수 (억원)
- `fetch_market_stocks(client, market)` : 시가총액 순위 페이지 전종목
- `fetch_index_daily_prices(client, index_code, count)` : fchart 일봉 OHLCV
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

import httpx
from bs4 import BeautifulSoup

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
    """외인/기관/개인 순매수 (억원). sosession: kospi=01, kosdaq=02."""
    sosession = "01" if market.lower() == "kospi" else "02"
    url = "https://finance.naver.com/sise/investorDealTrendDay.naver"
    try:
        resp = await client.get(
            url,
            headers=NAVER_HEADERS,
            params={"bizdate": bizdate, "sosession": sosession},
            timeout=10,
        )
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")

        table = soup.select_one("table.type_1")
        if not table:
            logger.warning("Naver investor table not found for %s", market)
            return None

        header_row = table.select_one("tr")
        if not header_row:
            return None
        headers = [th.get_text(strip=True) for th in header_row.select("th")]
        col_map: dict[str, int] = {}
        for i, h in enumerate(headers):
            if "외국인" in h:
                col_map["foreign"] = i
            elif h == "기관계":
                col_map["institutional"] = i
            elif "개인" in h:
                col_map["retail"] = i
        if not col_map:
            logger.warning("Naver investor header parse failed for %s", market)
            return None

        def _parse(tds: list, idx: int) -> float:
            if idx >= len(tds):
                return 0.0
            raw = tds[idx].get_text(strip=True).replace(",", "")
            raw = raw.replace("−", "-").replace("–", "-")
            if not raw or raw == "-":
                return 0.0
            try:
                return float(raw)
            except ValueError:
                return 0.0

        target_short = bizdate[2:]
        for row in table.select("tr")[1:]:
            tds = row.select("td")
            if not tds:
                continue
            row_date = tds[0].get_text(strip=True).replace(".", "")
            if row_date != target_short:
                continue
            trade_date = (
                date(int("20" + bizdate[2:4]), int(bizdate[4:6]), int(bizdate[6:8]))
                if len(bizdate) == 8
                else date.fromisoformat(bizdate)
            )
            return InvestorFlows(
                foreign_net=_parse(tds, col_map.get("foreign", 0)),
                institutional_net=_parse(tds, col_map.get("institutional", 0)),
                retail_net=_parse(tds, col_map.get("retail", 0)),
                trade_date=trade_date,
            )

        logger.warning("Naver investor: no row for date %s (%s)", bizdate, market)
        return None

    except Exception as e:
        logger.warning("Naver investor flows fetch failed (%s): %s", market, e)
        return None


# 네이버 investorDealTrendDay 컬럼명 → MarketInvestorBreakdown 필드. 헤더 텍스트에
# 부분일치로 매핑한다(투신(사모)·기타금융기관·연기금등 표기 변형 흡수). 충돌 없음 검증됨.
_INVESTOR_COL_KEYS: list[tuple[str, str]] = [
    ("개인", "individual_net"),
    ("외국인", "foreign_net"),
    ("기관계", "institution_net"),
    ("금융투자", "financial_inv_net"),
    ("보험", "insurance_net"),
    ("투신", "trust_net"),
    ("은행", "bank_net"),
    ("기타금융", "etc_finance_net"),
    ("연기금", "pension_net"),
    ("기타법인", "etc_corp_net"),
]


def _parse_net(raw: str) -> float:
    raw = raw.strip().replace(",", "").replace("−", "-").replace("–", "-")
    if not raw or raw == "-":
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


async def fetch_market_investor_breakdown(
    client: httpx.AsyncClient, market: str, bizdate: str
) -> list[MarketInvestorBreakdown]:
    """시장전체 일별 투자자유형별 순매수(연기금 분리). 한 페이지가 최근 ~20거래일을
    담으므로 행 전부를 반환한다(수집기가 일괄 upsert → 공백 self-heal). 단위는 네이버
    원시값(백만원), 부호=순매수. sosession: kospi=01, kosdaq=02."""
    sosession = "01" if market.lower() == "kospi" else "02"
    market_label = "KOSPI" if market.lower() == "kospi" else "KOSDAQ"
    url = "https://finance.naver.com/sise/investorDealTrendDay.naver"
    try:
        resp = await client.get(
            url,
            headers=NAVER_HEADERS,
            params={"bizdate": bizdate, "sosession": sosession},
            timeout=10,
        )
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.select_one("table.type_1")
        if not table:
            logger.warning("Naver investor breakdown: table not found (%s)", market)
            return []

        # 2단 헤더: row0 의 '기관'(colspan) 그룹을 row1 의 하위 컬럼으로 펼쳐 평탄화.
        header_rows = [tr for tr in table.select("tr") if tr.select("th")][:2]
        if len(header_rows) < 2:
            logger.warning("Naver investor breakdown: header rows missing (%s)", market)
            return []
        row0 = [th.get_text(strip=True) for th in header_rows[0].select("th")]
        row1 = [th.get_text(strip=True) for th in header_rows[1].select("th")]
        flat: list[str] = []
        for name in row0:
            if name == "기관":  # colspan 그룹 헤더 → 하위 컬럼으로 치환
                flat.extend(row1)
            else:
                flat.append(name)

        # 평탄화한 컬럼명 → 데이터 td 인덱스 → 필드. 0번은 날짜.
        idx_to_field: dict[int, str] = {}
        for i, name in enumerate(flat):
            for key, field in _INVESTOR_COL_KEYS:
                if key in name:
                    idx_to_field[i] = field
                    break
        if "pension_net" not in idx_to_field.values():
            logger.warning("Naver investor breakdown: 연기금 컬럼 미발견 (%s)", market)
            return []

        results: list[MarketInvestorBreakdown] = []
        for row in table.select("tr"):
            tds = row.select("td")
            if len(tds) < len(flat):
                continue
            date_txt = tds[0].get_text(strip=True).replace(".", "")  # "260623"
            if len(date_txt) != 6 or not date_txt.isdigit():
                continue
            trade_date = date(2000 + int(date_txt[0:2]), int(date_txt[2:4]), int(date_txt[4:6]))
            fields = {field: 0.0 for _, field in _INVESTOR_COL_KEYS}
            for i, field in idx_to_field.items():
                fields[field] = _parse_net(tds[i].get_text(strip=True))
            results.append(
                MarketInvestorBreakdown(trade_date=trade_date, market=market_label, **fields)
            )
        if not results:
            logger.warning("Naver investor breakdown: no data rows (%s, %s)", market, bizdate)
        return results
    except Exception as e:
        logger.warning("Naver investor breakdown fetch failed (%s): %s", market, e)
        return []


async def fetch_market_stocks(
    client: httpx.AsyncClient, market: str = "KOSPI", *, request_delay: float = 0.15
) -> list[MarketStock]:
    """시가총액 순위 페이지 전종목 (시총 억원 → 백만원 변환)."""
    sosok = "0" if market.upper() == "KOSPI" else "1"
    url = "https://finance.naver.com/sise/sise_market_sum.naver"
    stocks: list[MarketStock] = []
    seen: set[str] = set()

    for page in range(1, 100):
        try:
            resp = await client.get(
                url,
                headers=NAVER_HEADERS,
                params={"sosok": sosok, "page": str(page)},
                timeout=10,
            )
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.select_one("table.type_2")
            if not table:
                break

            page_count = 0
            for tr in table.select("tr"):
                tds = tr.select("td")
                if len(tds) < 7:
                    continue
                link = tr.select_one("a[href*='code=']")
                if not link:
                    continue
                href = link.get("href", "")
                code = href.split("code=")[-1].split("&")[0]
                if len(code) != 6 or not code.isdigit() or code in seen:
                    continue
                name = link.get_text(strip=True)
                if not name:
                    continue
                cap_text = tds[6].get_text(strip=True).replace(",", "")
                if not cap_text or cap_text == "-":
                    continue
                try:
                    cap_eok = int(cap_text)
                except ValueError:
                    continue
                seen.add(code)
                stocks.append(
                    MarketStock(
                        stock_code=code,
                        stock_name=name,
                        market_cap=cap_eok * 100,
                    )
                )
                page_count += 1

            if page_count == 0:
                break
            if page < 99:
                await asyncio.sleep(request_delay)
        except Exception as e:
            logger.warning("Naver market stocks page %d failed: %s", page, e)
            break

    logger.info("Naver market stocks (%s): %d", market, len(stocks))
    return stocks


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
