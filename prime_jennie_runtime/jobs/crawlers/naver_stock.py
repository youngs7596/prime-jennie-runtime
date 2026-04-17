"""네이버 금융 종목별 외국인/기관 수급 크롤러 — async 포팅.

v2 원본: `prime_jennie/infra/crawlers/naver_stock.py`. v3 어댑터 차이:
- sync `httpx.get` → async `client.get` (호출측 `AsyncClient` 주입)
- 페이지/파싱 로직은 동일 (frgn.naver, table.type2 두 번째 요약 "외국인" 매칭)
- 인코딩은 v2 와 동일하게 euc-kr
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


@dataclass
class StockFrgnRow:
    """종목별 외국인/기관 일별 수급 데이터."""

    trade_date: date
    close_price: int  # 종가 (원)
    inst_net_volume: int  # 기관 순매매량 (주)
    frgn_net_volume: int  # 외국인 순매매량 (주)
    frgn_holding_ratio: float  # 외국인 보유율 (%)


def _parse_signed_int(text: str) -> int:
    cleaned = text.replace(",", "").replace("+", "")
    cleaned = cleaned.replace("−", "-").replace("–", "-")
    if not cleaned or cleaned == "-":
        return 0
    return int(cleaned)


def _parse_int(text: str) -> int:
    cleaned = text.replace(",", "")
    if not cleaned or cleaned == "-":
        return 0
    return int(cleaned)


def _parse_float(text: str) -> float:
    cleaned = text.replace(",", "").replace("%", "")
    cleaned = cleaned.replace("−", "-").replace("–", "-")
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned)


def parse_frgn_table(html: str) -> list[StockFrgnRow]:
    """frgn.naver HTML 에서 외국인/기관 수급 테이블 파싱 (v2 동일)."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("table.type2")
    target: Tag | None = None
    for t in tables:
        summary = t.get("summary", "")
        if isinstance(summary, str) and "외국인" in summary:
            target = t
            break

    if target is None:
        return []

    rows: list[StockFrgnRow] = []
    for tr in target.select("tr"):
        tds = tr.select("td")
        if len(tds) < 9:
            continue

        date_text = tds[0].get_text(strip=True)
        if not re.match(r"\d{4}\.\d{2}\.\d{2}", date_text):
            continue

        try:
            trade_date = date.fromisoformat(date_text.replace(".", "-"))
            close_price = _parse_int(tds[1].get_text(strip=True))
            inst_net_volume = _parse_signed_int(tds[5].get_text(strip=True))
            frgn_net_volume = _parse_signed_int(tds[6].get_text(strip=True))
            frgn_holding_ratio = _parse_float(tds[8].get_text(strip=True))
        except (ValueError, IndexError) as e:
            logger.debug("Row parse error (%s): %s", date_text, e)
            continue

        rows.append(
            StockFrgnRow(
                trade_date=trade_date,
                close_price=close_price,
                inst_net_volume=inst_net_volume,
                frgn_net_volume=frgn_net_volume,
                frgn_holding_ratio=frgn_holding_ratio,
            )
        )

    return rows


async def fetch_stock_frgn_data(
    client: httpx.AsyncClient, stock_code: str
) -> list[StockFrgnRow] | None:
    """v2 `fetch_stock_frgn_data` 의 async 어댑터.

    실패 (네트워크/파싱 모두) 시 None 을 반환해 호출측이 skip 카운트로 처리.
    """
    url = f"https://finance.naver.com/item/frgn.naver?code={stock_code}&page=1"
    try:
        resp = await client.get(url, headers=NAVER_HEADERS, timeout=10.0)
        resp.encoding = "euc-kr"
        html = resp.text
        rows = parse_frgn_table(html)
        if not rows:
            logger.warning("[%s] no frgn data rows parsed", stock_code)
            return None
        return rows
    except Exception as e:
        logger.warning("[%s] naver frgn fetch failed: %s", stock_code, e)
        return None


__all__ = [
    "NAVER_HEADERS",
    "StockFrgnRow",
    "fetch_stock_frgn_data",
    "parse_frgn_table",
]
