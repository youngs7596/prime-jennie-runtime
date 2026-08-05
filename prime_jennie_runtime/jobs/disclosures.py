"""DART 공시 수집 job.

v2 원본: `/jobs/collect-dart-filings` (app.py:553-621). v3 어댑터:
- OpenDartReader (sync, pandas) → DART OpenAPI list.json 직접 호출 (async httpx).
- 외부 의존성 1개 제거. 응답 필드는 v2 가 사용하던 항목과 동일.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .crawlers.dart import fetch_dart_filings

logger = logging.getLogger(__name__)

DART_RECENT_DAYS = 7  # v2 와 동일 (최근 7일).

# 수집할 공시 유형 (2026-08-05 확대).
#
# v2 를 그대로 옮겨 오느라 정기공시(A)만 받고 있었다. 사업·반기보고서만 들어오고
# 유상증자·공급계약·잠정실적 같은 **수시공시가 통째로 빠져 있었다** — 정기공시는 주당
# 46건뿐이라 "하루 1~3건"이 정상으로 보였고 그래서 오래 안 드러났다.
#
# 상장사 기준 주간 실측(2026-08-05):
#   A 정기공시      46   사업·반기·분기보고서
#   B 주요사항보고 191   유상증자·전환사채·합병·소송 — 자본시장법상 수시공시
#   C 발행공시     471   증권신고서·투자설명서 (B 의 실제 발행 단계)
#   D 지분공시   1,108   대량보유상황·임원 소유상황 — 수급 신호
#   E 기타공시     107   주주총회 소집공고 등
#   I 거래소공시 1,324   잠정실적·단일판매공급계약·배당결정·기업설명회 — 거래소 수시공시
#   J 공정위공시    49   대규모기업집단 내부거래
# 빠진 셋은 상장사 행이 **실측 0** 이라 뺐다: F 외부감사관련 0, G 펀드공시 0(486건 전부
# 비상장), H 자산유동화 0. 추측이 아니라 같은 창으로 재 본 값이다.
#
# 유형 코드는 `report_type` 에 그대로 저장하므로 소비 쪽에서 골라 쓰면 된다.
DART_PUBLIC_TYPES = ("A", "B", "C", "D", "E", "I", "J")
DART_TITLE_MAX = 500
DART_REPORT_TYPE_MAX = 50
DART_CORP_NAME_MAX = 100


async def collect_dart_filings(
    pool: Any,
    http: httpx.AsyncClient,
    *,
    api_key: str,
    days: int = DART_RECENT_DAYS,
) -> None:
    """v2 `/jobs/collect-dart-filings` 포팅 + 수시공시 확대 (2026-08-05).

    최근 `days` 일 공시를 `DART_PUBLIC_TYPES` 유형별로 수집해 활성 종목과 매칭되는
    row 만 `stock_disclosures` 에 INSERT. UNIQUE(receipt_no) 로 중복 자연 차단.

    유형 하나가 비거나 실패해도 나머지는 계속 받는다 — 한 유형의 침묵이 전체 수집을
    끌어내리면 안 되고, 유형별 건수를 로그에 남겨야 어느 쪽이 조용해졌는지 보인다.
    """
    if not api_key:
        logger.warning("collect_dart_filings: DART_API_KEY 비어있음 — skip")
        return

    today = date.today()
    bgn_de = (today - timedelta(days=days)).strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    filings = []
    per_type: dict[str, int] = {}
    for pblntf_ty in DART_PUBLIC_TYPES:
        try:
            rows_of_type = await fetch_dart_filings(
                http,
                api_key=api_key,
                bgn_de=bgn_de,
                end_de=end_de,
                pblntf_ty=pblntf_ty,
            )
        except Exception as e:
            logger.warning("collect_dart_filings: 유형 %s 수집 실패 — %s", pblntf_ty, e)
            rows_of_type = []
        per_type[pblntf_ty] = len(rows_of_type)
        filings.extend(rows_of_type)

    if not filings:
        logger.info("collect_dart_filings: no filings (range=%s..%s)", bgn_de, end_de)
        return

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT stock_code FROM stock_masters WHERE is_active = TRUE")
        active_codes = {r["stock_code"] for r in rows}

        inserted = 0
        for f in filings:
            if not f.stock_code or f.stock_code not in active_codes:
                continue
            if not f.rcept_no:
                continue

            try:
                disc_date = datetime.strptime(f.rcept_dt, "%Y%m%d").date()
            except ValueError:
                disc_date = today

            result = await conn.execute(
                "INSERT INTO stock_disclosures "
                "(stock_code, disclosure_date, title, report_type, receipt_no, corp_name) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (receipt_no) DO NOTHING",
                f.stock_code,
                disc_date,
                f.report_nm[:DART_TITLE_MAX],
                (f.pblntf_ty[:DART_REPORT_TYPE_MAX] or None),
                f.rcept_no,
                (f.corp_name[:DART_CORP_NAME_MAX] or None),
            )
            if result.endswith(" 1"):
                inserted += 1

    logger.info(
        "collect_dart_filings: inserted=%d candidates=%d range=%s..%s 유형별=%s",
        inserted,
        len(filings),
        bgn_de,
        end_de,
        ", ".join(f"{t}:{n}" for t, n in per_type.items()),
    )


__all__ = [
    "DART_CORP_NAME_MAX",
    "DART_PUBLIC_TYPES",
    "DART_RECENT_DAYS",
    "DART_REPORT_TYPE_MAX",
    "DART_TITLE_MAX",
    "collect_dart_filings",
]
