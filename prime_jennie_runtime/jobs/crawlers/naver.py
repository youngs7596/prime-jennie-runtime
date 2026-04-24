"""네이버 금융 크롤러 — 종목 fundamentals/ROE/섹터 매핑.

v2 `prime_jennie/infra/crawlers/naver.py` 의 아래 함수를 async httpx 기반으로
옮긴다. 파싱 규칙(셀렉터, (E) 제외, 라벨 매칭)은 그대로 유지 — v2 에서 안정적
으로 통과하던 파라미터를 재해석하지 않는다.

- `crawl_naver_fundamentals(client, stock_code)` : 주요재무정보 테이블에서
  최근 실적 분기 PER/PBR/ROE
- `crawl_naver_roe(client, stock_code)` : 종목 메인 페이지 ROE row 마지막 유효값
- `build_naver_sector_mapping(client)` : 네이버 업종 분류 → {code: sector_name}

뉴스 크롤은 이미 `news_pipeline_kor/adapters/naver_crawler.py` 에 async 로
이식되어 있어 포팅 대상 아님.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

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

_DATE_RE = re.compile(r"\d{4}\.\d{2}")


@dataclass(frozen=True)
class NaverFundamentals:
    """주요재무정보 테이블에서 추출한 최신 실적 분기 지표."""

    per: float | None = None
    pbr: float | None = None
    roe: float | None = None
    quarter_name: str | None = None


async def _get_euckr_soup(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10.0,
) -> BeautifulSoup | None:
    try:
        resp = await client.get(
            url,
            params=params,
            headers={**NAVER_HEADERS, **(headers or {})},
            timeout=timeout,
        )
        resp.encoding = "euc-kr"
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.warning("GET %s failed: %s", url, e)
        return None


async def crawl_naver_roe(client: httpx.AsyncClient, stock_code: str) -> float | None:
    """네이버 금융 종목 메인 페이지에서 ROE(%) 파싱.

    테이블 구조: th 에 "ROE" 포함된 row 의 마지막 유효 td 값.
    """
    soup = await _get_euckr_soup(
        client, f"https://finance.naver.com/item/main.naver?code={stock_code}"
    )
    if soup is None:
        return None

    for table in soup.select("table"):
        for row in table.select("tr"):
            th = row.select_one("th")
            if not th or "ROE" not in th.get_text():
                continue
            best: float | None = None
            for td in row.select("td"):
                text = td.get_text(strip=True).replace(",", "")
                if not text or text in ("-", "N/A"):
                    continue
                try:
                    best = float(text)
                except ValueError:
                    continue
            if best is not None:
                return best
    return None


async def crawl_naver_fundamentals(
    client: httpx.AsyncClient, stock_code: str
) -> NaverFundamentals | None:
    """최신 실적(non-E) 분기의 PER/PBR/ROE.

    v2 규칙 유지:
    - EPS/BPS/PER 모두 포함된 table 을 주요재무정보 테이블로 간주
    - 날짜 행(`\\d{4}\\.\\d{2}`) 우측부터 탐색, "(E)" 없는 가장 최근 열 선택
    - PBR/BPS, PER/EPS 라벨 혼동 방지 (라벨에 둘 다 있으면 스킵)
    """
    soup = await _get_euckr_soup(
        client, f"https://finance.naver.com/item/main.naver?code={stock_code}"
    )
    if soup is None:
        return None

    target_table = None
    for table in soup.select("table"):
        text = table.get_text()
        if "EPS" in text and "BPS" in text and "PER" in text:
            target_table = table
            break
    if target_table is None:
        return None

    rows = target_table.select("tr")
    if not rows:
        return None

    actual_col_idx: int | None = None
    quarter_name: str | None = None
    for row in rows:
        ths = row.select("th")
        if not ths:
            continue
        header_texts = [th.get_text(strip=True) for th in ths]
        date_ths = [t for t in header_texts if _DATE_RE.match(t)]
        if len(date_ths) < 2:
            continue
        for i in range(len(ths) - 1, -1, -1):
            th_text = ths[i].get_text(strip=True)
            if _DATE_RE.match(th_text) and "(E)" not in th_text:
                actual_col_idx = i
                quarter_name = th_text
                break
        break

    if actual_col_idx is None:
        return None

    per_val: float | None = None
    pbr_val: float | None = None
    roe_val: float | None = None

    for row in rows:
        first = row.select_one("th")
        if not first:
            continue
        label = first.get_text(strip=True)
        tds = row.select("td")
        if not tds or actual_col_idx >= len(tds):
            continue
        raw = tds[actual_col_idx].get_text(strip=True).replace(",", "")
        if not raw or raw in ("-", "N/A"):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue

        if "PER" in label and "EPS" not in label:
            per_val = value
        elif "PBR" in label and "BPS" not in label:
            pbr_val = value
        elif "ROE" in label:
            roe_val = value

    if per_val is None and pbr_val is None and roe_val is None:
        return None

    return NaverFundamentals(per=per_val, pbr=pbr_val, roe=roe_val, quarter_name=quarter_name)


async def _get_sector_stocks(client: httpx.AsyncClient, sector_no: str) -> list[str]:
    soup = await _get_euckr_soup(
        client,
        "https://finance.naver.com/sise/sise_group_detail.naver",
        params={"type": "upjong", "no": sector_no},
    )
    if soup is None:
        return []
    codes: list[str] = []
    for link in soup.select("table.type_5 td a[href*='code=']"):
        href = link.get("href", "")
        if "code=" in href:
            code = href.split("code=")[-1].split("&")[0]
            if len(code) == 6 and code.isdigit():
                codes.append(code)
    return codes


async def build_naver_sector_mapping(
    client: httpx.AsyncClient, *, request_delay: float = 0.2
) -> dict[str, str]:
    """네이버 업종 분류 → {stock_code: sector_name}.

    v2 는 79개 세분류를 순회. sector 목록과 종목 상세는 별도 URL.
    """
    mapping: dict[str, str] = {}
    soup = await _get_euckr_soup(
        client,
        "https://finance.naver.com/sise/sise_group.naver",
        params={"type": "upjong"},
        timeout=15.0,
    )
    if soup is None:
        return mapping

    sector_links = soup.select("table.type_1 td a[href*='no=']")
    for link in sector_links:
        sector_name = link.get_text(strip=True)
        href = link.get("href", "")
        if "no=" not in href:
            continue
        sector_no = href.split("no=")[-1].split("&")[0]
        stocks = await _get_sector_stocks(client, sector_no)
        for code in stocks:
            mapping[code] = sector_name
        await asyncio.sleep(request_delay)

    logger.info("Naver sector mapping: %d stocks mapped", len(mapping))
    return mapping
