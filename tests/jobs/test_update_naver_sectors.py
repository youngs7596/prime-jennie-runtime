"""`update_naver_sectors` 스모크 — sector mapping 크롤 + stock_masters UPDATE.

v2 의 `SectorGroup` 대분류 로직(`get_sector_group`) 이 제대로 적용되는지,
UPDATE 가 매핑 개수만큼 실행되는지 확인.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from prime_jennie_runtime.jobs.maintenance import update_naver_sectors

_SECTOR_LIST_URL = r"https://m\.stock\.naver\.com/api/stocks/industry\?.*"
_SECTOR_DETAIL_URL = r"https://m\.stock\.naver\.com/api/stocks/industry/\d+.*"


_SECTOR_LIST_JSON = {
    "stockListSortType": "INDUSTRY",
    "groups": [
        {"no": 1, "name": "반도체와반도체장비", "totalCount": 1},
        {"no": 2, "name": "은행", "totalCount": 1},
    ],
    "totalCount": 2,
}


def _detail_json(codes: list[str]) -> dict:
    return {
        "stockListSortType": "INDUSTRY",
        "stocks": [{"itemCode": c, "stockName": c} for c in codes],
        "totalCount": len(codes),
    }


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.result: str = "UPDATE 1"

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return self.result


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_update_naver_sectors_maps_and_updates():
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        # 두 업종 상세 요청 — 각각 1종목씩. 목록보다 먼저 걸어야 경로가 안 겹친다.
        mock.get(url__regex=_SECTOR_DETAIL_URL).mock(
            side_effect=[
                httpx.Response(200, json=_detail_json(["005930"])),
                httpx.Response(200, json=_detail_json(["055550"])),
            ]
        )
        mock.get(url__regex=_SECTOR_LIST_URL).respond(200, json=_SECTOR_LIST_JSON)
        async with httpx.AsyncClient() as client:
            await update_naver_sectors(pool, client)

    # 최소 2건 UPDATE 실행 (code → sector).
    update_calls = [c for c in pool.conn.calls if "UPDATE stock_masters" in c[0]]
    assert len(update_calls) == 2

    mapped = {call[1][2]: (call[1][0], call[1][1]) for call in update_calls}
    # 005930 (삼성전자) → 반도체와반도체장비 → SEMICONDUCTOR_IT
    assert mapped["005930"] == ("반도체와반도체장비", "반도체/IT")
    # 055550 (신한지주) → 은행 → FINANCE
    assert mapped["055550"] == ("은행", "금융")


@pytest.mark.asyncio
async def test_update_naver_sectors_skips_on_empty_mapping(caplog):
    pool = _FakePool()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=_SECTOR_LIST_URL).respond(200, json={"groups": []})
        async with httpx.AsyncClient() as client:
            await update_naver_sectors(pool, client)

    assert not pool.conn.calls
