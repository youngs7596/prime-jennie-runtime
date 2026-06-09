"""FnGuide + Naver wisereport 컨센서스 크롤러 (async 포팅).

v2 `prime_jennie/infra/crawlers/fnguide.py` 의 파싱 규칙을 그대로 유지하고
HTTP 만 `httpx.AsyncClient` 로 바꿨다.

- `crawl_consensus(client, stock_code)` : FnGuide 우선, 실패 시 Naver 폴백.
  목표주가·추정기관수·투자의견·forward EPS/PER 은 '투자의견 컨센서스' 표(열 방향)에서,
  forward ROE 는 '업종 비교' 표 회사 컬럼에서 읽는다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


@dataclass
class ConsensusData:
    forward_per: float | None = None
    forward_eps: float | None = None
    forward_roe: float | None = None
    target_price: int | None = None
    analyst_count: int | None = None
    investment_opinion: float | None = None
    source: str = "FNGUIDE"


async def crawl_consensus(client: httpx.AsyncClient, stock_code: str) -> ConsensusData | None:
    """FnGuide → Naver 순서로 시도, 성공한 쪽 반환."""
    result = await crawl_fnguide_consensus(client, stock_code)
    source = "FNGUIDE"

    if result is None:
        result = await crawl_naver_consensus(client, stock_code)
        source = "NAVER"
    if result is None:
        return None

    # 추정기관수가 적어도(thin coverage) 레코드는 버리지 않는다. 컨센서스 추정치가
    # 얇아도 '업종 비교' 표에서 온 trailing EPS·ROE 는 그대로 쓸모가 있어서, 통째로
    # 드롭하면 그 펀더멘털까지 같이 사라진다 (2026-06-09 결정).
    result.source = source
    return result


async def crawl_fnguide_consensus(
    client: httpx.AsyncClient, stock_code: str
) -> ConsensusData | None:
    url = f"https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp?gicode=A{stock_code}"
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        result = ConsensusData()
        # '투자의견 컨센서스' 추정 표를 먼저 읽어 목표주가·추정기관수·투자의견과 진짜
        # forward EPS·PER 을 채운다. 그 다음 '업종 비교' 표가 forward ROE 를 채우고,
        # 커버 없는 종목에 한해 trailing EPS·PER 로 폴백한다 (이미 채워졌으면 건너뜀).
        _parse_fnguide_consensus_estimate(soup, result)
        _parse_fnguide_consensus_table(soup, result)

        if result.forward_per is None and result.forward_eps is None:
            return None
        return result
    except Exception as e:
        logger.debug("[%s] FnGuide consensus crawl failed: %s", stock_code, e)
        return None


def _parse_fnguide_consensus_table(soup: BeautifulSoup, result: ConsensusData) -> None:
    # 1) "업종 비교" 표 — EPS·ROE 는 회사 컬럼에서 읽는다.
    #    이 표의 헤더는 ['구분', <회사명>, '코스피 …(업종평균)', 'KOSPI(시장평균)'] 라서
    #    회사 값은 첫 데이터 셀(tds[0]) 이다. 이전 코드는 tds[-1] (KOSPI 시장평균) 를
    #    읽어 전 종목이 동일한 값으로 오염됐다 — forward_roe 가 모든 종목 8.84,
    #    forward_eps 가 9,060.56 로 찍히던 버그 (2026-06-07 수정).
    for table in soup.select("table"):
        caption = table.find("caption")
        if caption is None or "업종 비교" not in caption.get_text():
            continue
        for row in table.select("tr"):
            th = row.select_one("th")
            if not th:
                continue
            label = th.get_text(strip=True)
            tds = row.select("td")
            if not tds:
                continue
            company = _parse_number(tds[0].get_text(strip=True))
            if company is None:
                continue
            if label.startswith("EPS") and result.forward_eps is None:
                result.forward_eps = company
            elif label == "ROE" and result.forward_roe is None:
                result.forward_roe = company

    # 2) Financial Highlight 표 — PER 은 최우측 (E) 컬럼이 forward 추정치다.
    #    "업종 비교" 표의 PER 라벨엔 "배" 가 없어 자연히 건너뛰고 FH 의 'PER(배)' 행을
    #    잡는다 (기존에 유일하게 정상 동작하던 경로).
    for table in soup.select("table"):
        for row in table.select("tr"):
            th = row.select_one("th, td.cmp-table-cell")
            if not th:
                continue
            label = th.get_text(strip=True)
            tds = row.select("td")
            if not tds:
                continue
            if "PER" in label and "배" in label and result.forward_per is None:
                val = _parse_number(tds[-1].get_text(strip=True))
                if val is not None and val > 0:
                    result.forward_per = val

    for div in soup.select("div.corp_group2, div.corp_group1"):
        for table in div.select("table"):
            for row in table.select("tr"):
                ths = row.select("th")
                tds = row.select("td")
                if not ths or not tds:
                    continue
                for th, td in zip(ths, tds, strict=False):
                    label = th.get_text(strip=True)
                    val_text = td.get_text(strip=True)

                    if "PER" in label and result.forward_per is None:
                        val = _parse_number(val_text)
                        if val is not None and val > 0:
                            result.forward_per = val
                    if "EPS" in label and result.forward_eps is None:
                        val = _parse_number(val_text)
                        if val is not None:
                            result.forward_eps = val
                    if "ROE" in label and result.forward_roe is None:
                        val = _parse_number(val_text)
                        if val is not None:
                            result.forward_roe = val


def _parse_fnguide_consensus_estimate(soup: BeautifulSoup, result: ConsensusData) -> None:
    """'투자의견 컨센서스' 추정 표 — 열 방향이다.

    헤더 한 줄에 [투자의견, 목표주가, EPS, PER, 추정기관수] 가 나열되고 값은 그 아래
    데이터 한 줄에 들어간다. 목표주가·추정기관수·투자의견은 이 표에만 있고, 여기 EPS·PER
    이 증권사 평균 추정치(FY1)다 — '업종 비교' 표의 최근결산 회사 값보다 진짜 forward 다.

    이전 코드는 "목표주가" 를 행 머리(왼쪽 라벨)로 찾아서 영영 못 잡았다. 실제론 열 헤더라
    target_price·analyst_count 가 전 종목 None 으로 비어 있었다 (2026-06-09 수정).

    커버 없는 종목은 데이터 자리에 "관련 데이터가 없습니다" 한 칸만 찍히므로 건너뛴다.
    """
    for table in soup.select("table"):
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        if not any("목표주가" in h for h in headers):
            continue
        row = table.select_one("tbody tr")
        if row is None:
            return
        cells = row.select("td")
        if len(cells) < len(headers):  # "관련 데이터가 없습니다" placeholder
            return
        for header, cell in zip(headers, cells, strict=False):
            val = _parse_number(cell.get_text(strip=True))
            if val is None:
                continue
            if "투자의견" in header and result.investment_opinion is None and 1 <= val <= 5:
                result.investment_opinion = val
            elif "목표주가" in header and result.target_price is None and val > 0:
                result.target_price = int(val)
            elif header == "EPS" and result.forward_eps is None:
                result.forward_eps = val
            elif "PER" in header and result.forward_per is None and val > 0:
                result.forward_per = val
            elif "기관수" in header and result.analyst_count is None and val > 0:
                result.analyst_count = int(val)
        return


async def crawl_naver_consensus(client: httpx.AsyncClient, stock_code: str) -> ConsensusData | None:
    url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={stock_code}"
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        result = ConsensusData()
        _parse_naver_consensus_page(soup, result)

        if result.forward_per is None and result.forward_eps is None:
            return None
        return result
    except Exception as e:
        logger.debug("[%s] Naver consensus crawl failed: %s", stock_code, e)
        return None


def _parse_naver_consensus_page(soup: BeautifulSoup, result: ConsensusData) -> None:
    for table in soup.select("table"):
        for row in table.select("tr"):
            ths = row.select("th")
            tds = row.select("td")
            if not ths or not tds:
                continue
            for th in ths:
                label = th.get_text(strip=True)
                for td in reversed(tds):
                    val_text = td.get_text(strip=True)
                    if not val_text or val_text in ("-", "N/A"):
                        continue
                    if any(c in val_text for c in ("주주", "자본", "순이익", "당기")):
                        continue
                    val = _parse_number(val_text)
                    if val is None:
                        continue
                    if "EPS" in label and result.forward_eps is None:
                        result.forward_eps = val
                    elif "PER" in label and result.forward_per is None and val > 0:
                        result.forward_per = val
                    elif "ROE" in label and result.forward_roe is None:
                        result.forward_roe = val
                    break

    for dl in soup.select("dl, div.cmp_comment"):
        text = dl.get_text()
        if "목표주가" in text:
            val = _extract_number_after(text, "목표주가")
            if val and val > 0:
                result.target_price = int(val)
        if "투자의견" in text:
            val = _extract_number_after(text, "투자의견")
            if val and 1 <= val <= 5:
                result.investment_opinion = val

    for pat in soup.find_all(string=re.compile(r"\d+명")):
        parent = pat.find_parent()
        if parent and ("애널리스트" in parent.get_text() or "기관" in parent.get_text()):
            match = re.search(r"(\d+)명", pat)
            if match:
                result.analyst_count = int(match.group(1))
                break


def _parse_number(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.replace(",", "").replace(" ", "").strip()
    match = re.search(r"[+-]?\d+\.?\d*", cleaned)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return None
    return None


def _extract_number_after(text: str, keyword: str) -> float | None:
    idx = text.find(keyword)
    if idx < 0:
        return None
    return _parse_number(text[idx + len(keyword) :])
