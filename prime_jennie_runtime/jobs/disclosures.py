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

DART_RECENT_DAYS = 7  # v2 와 동일 (최근 7일 정기공시).
DART_PUBLIC_TYPE = "A"  # v2 OpenDartReader kind="A" → pblntf_ty="A".
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
    """v2 `/jobs/collect-dart-filings` 포팅.

    최근 `days` 일 정기공시 (pblntf_ty=A) 를 수집해 활성 종목과 매칭되는 row 만
    `stock_disclosures` 에 INSERT. UNIQUE(receipt_no) 로 중복 자연 차단.
    """
    if not api_key:
        logger.warning("collect_dart_filings: DART_API_KEY 비어있음 — skip")
        return

    today = date.today()
    bgn_de = (today - timedelta(days=days)).strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    filings = await fetch_dart_filings(
        http,
        api_key=api_key,
        bgn_de=bgn_de,
        end_de=end_de,
        pblntf_ty=DART_PUBLIC_TYPE,
    )
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
        "collect_dart_filings: inserted=%d candidates=%d range=%s..%s",
        inserted,
        len(filings),
        bgn_de,
        end_de,
    )


__all__ = [
    "DART_CORP_NAME_MAX",
    "DART_PUBLIC_TYPE",
    "DART_RECENT_DAYS",
    "DART_REPORT_TYPE_MAX",
    "DART_TITLE_MAX",
    "collect_dart_filings",
]
