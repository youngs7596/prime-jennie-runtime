"""FnGuide + Naver wisereport 컨센서스 크롤러 (async 포팅).

v2 `prime_jennie/infra/crawlers/fnguide.py` 의 파싱 규칙을 그대로 유지하고
HTTP 만 `httpx.AsyncClient` 로 바꿨다.

- `crawl_consensus(client, stock_code)` : FnGuide 우선, 실패 시 Naver 폴백.
  목표주가·추정기관수·투자의견·forward EPS/PER 은 '투자의견' 표(열 방향)에서,
  forward ROE 는 업종비교 위젯이 부르는 JSON 에서 읽는다.

**2026-08-05 이전 기록 — 종목이 뒤바뀐 채 5주를 흘렀다.**
FnGuide 가 기업정보를 `comp.fnguide.com/SVO2/ASP/SVD_Main.asp?gicode=A종목코드` 에서
`wcomp.fnguide.com/CompanyInfo/Snapshot?cmp_cd=A종목코드` 로 옮겼다. 옛 주소는 지금
"페이지가 없습니다" 안내문을 **HTTP 200 으로** 준다. 이사 직후에는 더 나빴다 — 옛 주소가
새 사이트로 넘어가면서 파라미터 이름 `gicode` 가 무시됐고, 새 사이트는 종목을 못 알아들으면
**기본 페이지인 삼성전자를 보여준다.** 그래서 7-02~7-27 여덟 거래일 동안 213 종목이 전부
삼성전자 숫자(목표주가 513,958 · 추정기관수 24 · EPS 46,664 · PER 5.3)를 받아 적혔다.
빈 값이 아니라 그럴듯한 값이라 아무 데서도 안 걸렸다.

그래서 **페이지가 스스로 밝힌 종목코드를 요청한 코드와 대조한다**(`_page_stock_code`).
값이 몇 가지냐를 사후에 세는 것보다 이쪽이 정확하다 — 한 종목만 조회해도 즉시 걸리고,
우선주처럼 조용히 보통주로 바꿔치기되는 경우(A001527 을 넣으면 동양(001520) 이 온다)도
같은 검사로 잡힌다. 우선주는 컨센서스가 따로 없으니 걸러 내는 쪽이 맞다.
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

_SNAPSHOT_URL = "https://wcomp.fnguide.com/CompanyInfo/Snapshot?cmp_cd=A{code}"
# 업종비교 위젯이 부르는 JSON. prc_typ 4=ROE (1=EPS, 2=PER, 3=EV/EBITDA, 5=배당수익률).
# 이쪽은 종목코드를 **A 접두어 없이** 받는다 — 붙이면 전 필드 null 인 빈 응답이 온다.
_SECTOR_ROE_URL = "https://wcomp.fnguide.com/CompanyInfo/getSnpSectorChart"

# 페이지 제목이 "삼성전자(005930) | Snapshot | 기업정보 | Company Guide" 꼴이다.
_TITLE_CODE_RE = re.compile(r"\((\d{6})\)")


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
    url = _SNAPSHOT_URL.format(code=stock_code)
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        # 받아 온 페이지가 정말 그 종목인지부터 확인한다. 다른 종목이 왔는데 파싱만
        # 성공하면 남의 숫자가 이 종목 행에 적힌다 (모듈 상단 2026-08-05 기록).
        page_code = _page_stock_code(soup)
        if page_code != stock_code:
            logger.warning(
                "[%s] FnGuide 가 다른 종목을 돌려줬다 (page=%s) — 버린다",
                stock_code,
                page_code or "미상",
            )
            return None

        result = ConsensusData()
        # '투자의견' 표가 목표주가·추정기관수·투자의견과 증권사 평균 EPS·PER 을 한 줄에 준다.
        _parse_fnguide_consensus_estimate(soup, result)

        if result.forward_per is None and result.forward_eps is None:
            return None

        result.forward_roe = await _fetch_forward_roe(client, stock_code)
        return result
    except Exception as e:
        logger.debug("[%s] FnGuide consensus crawl failed: %s", stock_code, e)
        return None


def _page_stock_code(soup: BeautifulSoup) -> str | None:
    """페이지가 스스로 밝힌 종목코드. 못 읽으면 None (= 대조 실패로 취급)."""
    title = soup.select_one("title")
    if title is None:
        return None
    match = _TITLE_CODE_RE.search(title.get_text(strip=True))
    return match.group(1) if match else None


async def _fetch_forward_roe(client: httpx.AsyncClient, stock_code: str) -> float | None:
    """업종비교 위젯 JSON 에서 회사의 forward ROE 를 읽는다.

    응답은 [회사, 업종, 코스피 업종, 코스피] 네 줄이고 **첫 줄이 회사**다. 마지막 줄인
    시장평균을 읽어 전 종목이 같은 값으로 오염됐던 사고가 2026-06-07 에 있었으니, 첫 줄을
    집는다는 점을 바꾸지 말 것.

    열은 ['24, '25, '26E] 처럼 연도별인데 이름이 E 로 끝나는 열이 추정치다. 그 열이 없으면
    (추정 없는 종목) 마지막 실적 열로 떨어진다. 실패해도 컨센서스 레코드 자체는 살린다.
    """
    try:
        resp = await client.get(
            _SECTOR_ROE_URL,
            params={"cmp_cd": stock_code, "consol_typ": "C", "prc_typ": "4"},
            headers=_HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        dataset = resp.json().get("dataset") or {}
        rows = dataset.get("data") or []
        headers = dataset.get("header") or []
        if not rows or not rows[0].get("CMP_NM"):
            return None

        value_ids = [h.get("ID") for h in headers if str(h.get("ID", "")).startswith("VAL")]
        if not value_ids:
            return None
        estimate_ids = [
            h.get("ID")
            for h in headers
            if str(h.get("ID", "")).startswith("VAL") and str(h.get("NM", "")).endswith("E")
        ]
        column = estimate_ids[-1] if estimate_ids else value_ids[-1]

        value = rows[0].get(column)
        return float(value) if value is not None else None
    except Exception as e:
        logger.debug("[%s] FnGuide forward ROE fetch failed: %s", stock_code, e)
        return None


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
