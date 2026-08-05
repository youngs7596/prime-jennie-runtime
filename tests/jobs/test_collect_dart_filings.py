"""`collect_dart_filings` 스모크 — DART list.json 모킹 + INSERT ON CONFLICT."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.disclosures import DART_PUBLIC_TYPES, collect_dart_filings

_DART_URL_RE = r"https://opendart\.fss\.or\.kr/api/list\.json.*"

_NO_DATA = {"status": "013", "message": "조회된 데이터가 없습니다."}


def _list_payload(rows: list[dict], total_page: int = 1) -> dict:
    return {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "page_count": 100,
        "total_count": len(rows),
        "total_page": total_page,
        "list": rows,
    }


def _type_router(pages_by_type: dict[str, list[list[dict]]]):
    """요청한 공시 유형·페이지에 맞는 응답을 돌려주는 respx side_effect.

    수집이 유형별로 여러 번 호출되므로(2026-08-05 수시공시 확대), 어느 유형을 물었는지
    무시하고 같은 payload 를 돌려주면 유형 수만큼 중복 적재된 것처럼 보인다.
    등록 안 된 유형은 DART 가 실제로 주는 "조회 결과 없음"(013)으로 답한다.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        pblntf_ty = request.url.params.get("pblntf_ty", "")
        page_no = int(request.url.params.get("page_no", "1"))
        pages = pages_by_type.get(pblntf_ty) or []
        if not pages or page_no > len(pages):
            return httpx.Response(200, json=_NO_DATA)
        return httpx.Response(200, json=_list_payload(pages[page_no - 1], total_page=len(pages)))

    return _handler


def _row(stock_code: str, *, rcept_no: str, name: str, rcept_dt: str = "20260417"):
    """DART list.json row 실제 모양 (2026-08-05 실측).

    **공시 유형(pblntf_ty)이 응답에 없다** — 그래서 오래 `report_type` 이 전부 NULL 이었다.
    유형은 우리가 물어본 조회 조건이라 크롤러가 요청값을 찍는다. 이 픽스처에 유형을
    넣지 않는 게 그 계약을 지키는 부분이다.
    """
    return {
        "corp_code": "00126380",
        "stock_code": stock_code,
        "corp_name": "테스트회사",
        "corp_cls": "Y",
        "report_nm": name,
        "rcept_no": rcept_no,
        "flr_nm": "테스트회사",
        "rcept_dt": rcept_dt,
        "rm": "",
    }


class _FakeConn:
    def __init__(self, active_codes: list[str]) -> None:
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self._active_codes = active_codes

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.fetch_calls.append((sql, args))
        return [{"stock_code": c} for c in self._active_codes]

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, active_codes: list[str]) -> None:
        self.conn = _FakeConn(active_codes)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_collect_dart_filings_inserts_only_active_codes():
    pool = _FakePool(["005930", "000660"])  # 035720 은 활성 외
    router = _type_router(
        {
            "A": [
                [
                    _row("005930", rcept_no="20260417000123", name="분기보고서 (2026.03)"),
                    _row("035720", rcept_no="20260417000999", name="분기보고서"),  # 활성 외
                    _row("000660", rcept_no="20260417000456", name="주요사항보고서"),
                ]
            ]
        }
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_DART_URL_RE).mock(side_effect=router)
        async with httpx.AsyncClient() as client:
            await collect_dart_filings(pool, client, api_key="dummy", days=7)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_disclosures" in c[0]]
    assert len(inserts) == 2
    codes = sorted(c[1][0] for c in inserts)
    assert codes == ["000660", "005930"]
    # 005930 row 의 disclosure_date 가 rcept_dt 파싱 결과와 일치
    samsung = next(c for c in inserts if c[1][0] == "005930")
    assert samsung[1][1] == datetime.strptime("20260417", "%Y%m%d").date()
    assert samsung[1][4] == "20260417000123"  # receipt_no


@pytest.mark.asyncio
async def test_collects_every_configured_disclosure_type():
    """정기공시만 받던 것을 수시공시까지 넓혔다 (2026-08-05).

    v2 포팅 그대로 pblntf_ty=A 만 물어서 사업·반기보고서만 들어오고 유상증자·공급계약·
    잠정실적이 통째로 빠져 있었다. 정기공시가 주당 46건뿐이라 "하루 1~3건"이 정상으로
    보였던 게 오래 안 드러난 이유다.
    """
    pool = _FakePool(["005930"])
    router = _type_router(
        {
            "A": [[_row("005930", rcept_no="A0001", name="반기보고서")]],
            "B": [[_row("005930", rcept_no="B0001", name="주요사항보고서(유상증자결정)")]],
            "I": [[_row("005930", rcept_no="I0001", name="단일판매ㆍ공급계약체결")]],
        }
    )
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=_DART_URL_RE).mock(side_effect=router)
        async with httpx.AsyncClient() as client:
            await collect_dart_filings(pool, client, api_key="dummy", days=7)

    asked = {call.request.url.params.get("pblntf_ty") for call in route.calls}
    assert asked == set(DART_PUBLIC_TYPES)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_disclosures" in c[0]]
    assert sorted(c[1][4] for c in inserts) == ["A0001", "B0001", "I0001"]
    # 유형 코드가 report_type 으로 남아야 소비 쪽에서 골라 쓸 수 있다.
    assert sorted(c[1][3] for c in inserts) == ["A", "B", "I"]


@pytest.mark.asyncio
async def test_one_empty_type_does_not_stop_the_others():
    """유형 하나가 비어도 나머지는 계속 받는다 — 한 유형의 침묵이 전체를 끌어내리면 안 된다."""
    pool = _FakePool(["005930"])
    router = _type_router({"I": [[_row("005930", rcept_no="I0002", name="현금ㆍ현물배당결정")]]})
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_DART_URL_RE).mock(side_effect=router)
        async with httpx.AsyncClient() as client:
            await collect_dart_filings(pool, client, api_key="dummy", days=7)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_disclosures" in c[0]]
    assert [c[1][4] for c in inserts] == ["I0002"]


@pytest.mark.asyncio
async def test_collect_dart_filings_skip_when_no_api_key(caplog):
    pool = _FakePool(["005930"])
    async with httpx.AsyncClient() as client:
        await collect_dart_filings(pool, client, api_key="", days=7)
    assert pool.conn.execute_calls == []
    assert pool.conn.fetch_calls == []


@pytest.mark.asyncio
async def test_collect_dart_filings_handles_status_013():
    pool = _FakePool(["005930"])
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_DART_URL_RE).respond(
            200, json={"status": "013", "message": "조회된 데이터가 없습니다."}
        )
        async with httpx.AsyncClient() as client:
            await collect_dart_filings(pool, client, api_key="dummy", days=7)

    assert pool.conn.execute_calls == []


@pytest.mark.asyncio
async def test_collect_dart_filings_paginates():
    pool = _FakePool(["005930"])
    router = _type_router(
        {
            "A": [
                [_row("005930", rcept_no="20260417000001", name="분기보고서A")],
                [
                    _row(
                        "005930", rcept_no="20260418000002", name="분기보고서B", rcept_dt="20260418"
                    )
                ],
            ]
        }
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_DART_URL_RE).mock(side_effect=router)
        async with httpx.AsyncClient() as client:
            await collect_dart_filings(pool, client, api_key="dummy", days=7)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_disclosures" in c[0]]
    assert len(inserts) == 2
    receipt_nos = sorted(c[1][4] for c in inserts)
    assert receipt_nos == ["20260417000001", "20260418000002"]


@pytest.mark.asyncio
async def test_collect_dart_filings_invalid_rcept_dt_falls_back_to_today():
    pool = _FakePool(["005930"])
    router = _type_router(
        {"A": [[_row("005930", rcept_no="20260417000777", name="잘못된 날짜", rcept_dt="INVALID")]]}
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_DART_URL_RE).mock(side_effect=router)
        async with httpx.AsyncClient() as client:
            await collect_dart_filings(pool, client, api_key="dummy", days=7)

    inserts = [c for c in pool.conn.execute_calls if "INSERT INTO stock_disclosures" in c[0]]
    assert len(inserts) == 1
    assert inserts[0][1][1] == date.today()
