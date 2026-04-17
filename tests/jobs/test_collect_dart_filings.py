"""`collect_dart_filings` 스모크 — DART list.json 모킹 + INSERT ON CONFLICT."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.disclosures import collect_dart_filings

_DART_URL_RE = r"https://opendart\.fss\.or\.kr/api/list\.json.*"


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
    payload = _list_payload(
        [
            {
                "corp_code": "00126380",
                "stock_code": "005930",
                "corp_name": "삼성전자",
                "report_nm": "분기보고서 (2026.03)",
                "rcept_no": "20260417000123",
                "rcept_dt": "20260417",
                "pblntf_ty": "A",
            },
            {
                "corp_code": "00164779",
                "stock_code": "035720",  # 활성 외 → skip
                "corp_name": "카카오",
                "report_nm": "분기보고서",
                "rcept_no": "20260417000999",
                "rcept_dt": "20260417",
                "pblntf_ty": "A",
            },
            {
                "corp_code": "00164742",
                "stock_code": "000660",
                "corp_name": "SK하이닉스",
                "report_nm": "주요사항보고서",
                "rcept_no": "20260417000456",
                "rcept_dt": "20260417",
                "pblntf_ty": "A",
            },
        ]
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_DART_URL_RE).respond(200, json=payload)
        async with httpx.AsyncClient() as client:
            await collect_dart_filings(pool, client, api_key="dummy", days=7)

    inserts = [
        c for c in pool.conn.execute_calls if "INSERT INTO stock_disclosures" in c[0]
    ]
    assert len(inserts) == 2
    codes = sorted(c[1][0] for c in inserts)
    assert codes == ["000660", "005930"]
    # 005930 row 의 disclosure_date 가 rcept_dt 파싱 결과와 일치
    samsung = next(c for c in inserts if c[1][0] == "005930")
    assert samsung[1][1] == datetime.strptime("20260417", "%Y%m%d").date()
    assert samsung[1][4] == "20260417000123"  # receipt_no


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
    page1 = _list_payload(
        [
            {
                "corp_code": "00126380",
                "stock_code": "005930",
                "corp_name": "삼성전자",
                "report_nm": "분기보고서A",
                "rcept_no": "20260417000001",
                "rcept_dt": "20260417",
                "pblntf_ty": "A",
            }
        ],
        total_page=2,
    )
    page2 = _list_payload(
        [
            {
                "corp_code": "00126380",
                "stock_code": "005930",
                "corp_name": "삼성전자",
                "report_nm": "분기보고서B",
                "rcept_no": "20260418000002",
                "rcept_dt": "20260418",
                "pblntf_ty": "A",
            }
        ],
        total_page=2,
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_DART_URL_RE).mock(
            side_effect=[
                httpx.Response(200, json=page1),
                httpx.Response(200, json=page2),
            ]
        )
        async with httpx.AsyncClient() as client:
            await collect_dart_filings(pool, client, api_key="dummy", days=7)

    inserts = [
        c for c in pool.conn.execute_calls if "INSERT INTO stock_disclosures" in c[0]
    ]
    assert len(inserts) == 2
    receipt_nos = sorted(c[1][4] for c in inserts)
    assert receipt_nos == ["20260417000001", "20260418000002"]


@pytest.mark.asyncio
async def test_collect_dart_filings_invalid_rcept_dt_falls_back_to_today():
    pool = _FakePool(["005930"])
    payload = _list_payload(
        [
            {
                "corp_code": "00126380",
                "stock_code": "005930",
                "corp_name": "삼성전자",
                "report_nm": "잘못된 날짜",
                "rcept_no": "20260417000777",
                "rcept_dt": "INVALID",
                "pblntf_ty": "A",
            }
        ]
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_DART_URL_RE).respond(200, json=payload)
        async with httpx.AsyncClient() as client:
            await collect_dart_filings(pool, client, api_key="dummy", days=7)

    inserts = [
        c for c in pool.conn.execute_calls if "INSERT INTO stock_disclosures" in c[0]
    ]
    assert len(inserts) == 1
    assert inserts[0][1][1] == date.today()
