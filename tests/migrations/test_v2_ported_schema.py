"""v2 포팅 SQL 마이그레이션 (006-010) 정적 검증.

실 Postgres 없이 파일 내용 기반. apply/rollback 통합 테스트는 test_apply_rollback.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

PORTED_FILES = [
    "006_v2_masters.sql",
    "007_v2_market_data.sql",
    "008_v2_macro.sql",
    "009_v2_trading_ops.sql",
    "010_v2_legacy_news_trade.sql",
]

# MariaDB 특화 타입/구문 — 하나도 나오면 안 됨.
MARIADB_SPECIFIC_PATTERNS = [
    r"\bUNSIGNED\b",
    r"\bMEDIUMTEXT\b",
    r"\bTINYINT\b",
    r"\bAUTO_INCREMENT\b",
    r"\bON UPDATE CURRENT_TIMESTAMP\b",
    r"\bENGINE\s*=",
    r"\bCHARSET\s*=",
    r"\bCOLLATE\s*=\s*utf8",
]

# PK autoincrement 용 타입 — MariaDB AUTO_INCREMENT → Postgres BIGSERIAL/SERIAL
POSTGRES_SERIAL_TYPES = ("BIGSERIAL", "SERIAL")


@pytest.fixture(scope="module")
def migration_files() -> dict[str, str]:
    return {name: (MIGRATIONS_DIR / name).read_text() for name in PORTED_FILES}


def test_all_ported_files_exist():
    for name in PORTED_FILES:
        p = MIGRATIONS_DIR / name
        assert p.exists(), f"missing migration: {name}"


def test_no_mariadb_specific_syntax(migration_files):
    for name, text in migration_files.items():
        for pat in MARIADB_SPECIFIC_PATTERNS:
            m = re.search(pat, text, re.IGNORECASE)
            assert m is None, (
                f"{name}: MariaDB-specific pattern {pat!r} found at {m.start() if m else ''}"
            )


def test_boolean_defaults_are_postgres_style(migration_files):
    """v2 alembic 은 `server_default=sa.text('1')` 였음. Postgres 는 TRUE/FALSE 필요."""
    for name, text in migration_files.items():
        # BOOLEAN ... DEFAULT '1' 또는 DEFAULT '0' 패턴이 있으면 안 됨.
        bad = re.findall(r"BOOLEAN[^,)]*DEFAULT\s+'[01]'", text, re.IGNORECASE)
        assert not bad, f"{name}: bool default with string literal: {bad}"
        # BOOLEAN DEFAULT TRUE 또는 DEFAULT FALSE 만 허용.
        for m in re.finditer(r"BOOLEAN[^,)]*DEFAULT\s+(\w+)", text, re.IGNORECASE):
            val = m.group(1).upper()
            assert val in {"TRUE", "FALSE"}, f"{name}: unexpected BOOLEAN DEFAULT {val}"


def test_datetime_columns_use_timestamptz(migration_files):
    """v3 컨벤션: TIMESTAMP 대신 TIMESTAMPTZ."""
    for name, text in migration_files.items():
        # "TIMESTAMP" 중 "TIMESTAMPTZ" 가 아닌 단독 TIMESTAMP 를 잡음.
        bad = re.findall(r"\bTIMESTAMP\b(?!TZ)", text)
        assert not bad, f"{name}: bare TIMESTAMP (need TIMESTAMPTZ): {bad}"


def test_idempotent_create_statements(migration_files):
    """모든 CREATE TABLE / INDEX 는 IF NOT EXISTS."""
    for name, text in migration_files.items():
        for stmt in re.finditer(
            r"CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX)\s+(\w+)", text, re.IGNORECASE
        ):
            # 앞에 IF NOT EXISTS 가 있어야 함
            prefix_window = text[max(0, stmt.start() - 50) : stmt.start() + 100]
            assert "IF NOT EXISTS" in prefix_window.upper(), (
                f"{name}: non-idempotent CREATE: {stmt.group(0)}"
            )


# ─────────────────────────────────────────────────────────────────────
# 각 파일이 기대 테이블을 만드는지.
# ─────────────────────────────────────────────────────────────────────

EXPECTED_TABLES = {
    "006_v2_masters.sql": {"stock_masters", "configs"},
    "007_v2_market_data.sql": {
        "stock_investor_tradings",
        "stock_fundamentals",
        "stock_disclosures",
        "stock_consensus",
        "index_daily_prices",
        "us_market_daily",
    },
    "008_v2_macro.sql": {"daily_macro_insights", "global_macro_snapshots"},
    "009_v2_trading_ops.sql": {
        "positions",
        "daily_asset_snapshots",
        "watchlist_histories",
        "daily_quant_scores",
    },
    "010_v2_legacy_news_trade.sql": {"legacy_news_sentiments"},
}


def _extract_created_tables(text: str) -> set[str]:
    return {
        m.group(1).lower()
        for m in re.finditer(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", text, re.IGNORECASE)
    }


@pytest.mark.parametrize(("name", "expected"), list(EXPECTED_TABLES.items()))
def test_file_creates_expected_tables(migration_files, name, expected):
    actual = _extract_created_tables(migration_files[name])
    assert actual == expected, f"{name}: tables mismatch (got {actual})"


# ─────────────────────────────────────────────────────────────────────
# 핵심 컬럼/제약 — v2 alembic 원본과 동등한지.
# ─────────────────────────────────────────────────────────────────────


def test_stock_masters_has_expected_columns(migration_files):
    text = migration_files["006_v2_masters.sql"]
    for col in ("stock_code", "stock_name", "market", "sector_group", "is_active", "updated_at"):
        assert col in text, f"stock_masters missing column {col}"
    # market default KOSPI
    assert "'KOSPI'" in text


def test_watchlist_histories_has_v2_005_and_008_columns(migration_files):
    text = migration_files["009_v2_trading_ops.sql"]
    # v2 005
    assert "quant_score" in text
    assert "sector_group" in text
    assert "market_regime" in text
    # v2 008: run_id, is_active + PK 확장
    assert "run_id" in text
    assert "is_active" in text
    assert "pk_watchlist_histories" in text
    assert "(snapshot_date, stock_code, run_id)" in text


def test_daily_quant_scores_has_v2_006_and_008_columns(migration_files):
    text = migration_files["009_v2_trading_ops.sql"]
    assert "llm_grade" in text  # v2 006
    assert "run_id" in text  # v2 008
    assert "uq_quant_date_code_run" in text
    assert "ix_quant_active" in text


def test_daily_macro_insights_has_v2_002_added_columns(migration_files):
    text = migration_files["008_v2_macro.sql"]
    # v2 002_add_macro_dashboard_fields 에서 추가된 10 컬럼 전부 포함.
    for col in (
        "trading_reasoning",
        "council_consensus",
        "strategies_to_favor_json",
        "strategies_to_avoid_json",
        "opportunity_factors_json",
        "kospi_change_pct",
        "kosdaq_change_pct",
        "kospi_foreign_net",
        "kospi_institutional_net",
        "kospi_retail_net",
    ):
        assert col in text, f"daily_macro_insights missing 002 column {col}"


def test_stock_disclosures_uses_v2_004_confirmed_column_names(migration_files):
    """v2 004 에서 REPORT_CODE→report_type, COMPANY_NAME→corp_name 확정."""
    text = migration_files["007_v2_market_data.sql"]
    assert "report_type" in text
    assert "corp_name" in text
    # 구 컬럼명이 나오면 안 됨
    assert "REPORT_CODE" not in text
    assert "COMPANY_NAME" not in text


def test_legacy_news_sentiments_is_archival_only(migration_files):
    """legacy_news_sentiments 는 v3 운영 news_sentiments 와 별개 (다른 컬럼셋)."""
    text = migration_files["010_v2_legacy_news_trade.sql"]
    # v2 stock_news_sentiments 고유 컬럼.
    for col in ("press", "headline", "sentiment_reason", "article_url", "published_at"):
        assert col in text
    assert "imported_at" in text  # 아카이브 마커


# ─────────────────────────────────────────────────────────────────────
# 권한 패턴 — 001 전역 정책 따르는지.
# ─────────────────────────────────────────────────────────────────────


def test_all_files_grant_to_runtime(migration_files):
    for name, text in migration_files.items():
        assert "TO pj_runtime" in text, f"{name}: missing GRANT TO pj_runtime"
        assert "pj_meta" in text, f"{name}: missing GRANT to pj_meta"
        assert "pj_ui" in text, f"{name}: missing GRANT to pj_ui"


def test_serial_sequences_grant_usage(migration_files):
    """BIGSERIAL PK 를 쓰는 테이블은 해당 _{col}_seq 에 USAGE GRANT 필요."""
    # CREATE TABLE IF NOT EXISTS <tname> ( ... <col> BIGSERIAL PRIMARY KEY ... )
    create_re = re.compile(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )
    col_re = re.compile(r"(\w+)\s+BIGSERIAL\s+PRIMARY\s+KEY", re.IGNORECASE)
    for name, text in migration_files.items():
        for m in create_re.finditer(text):
            tname, body = m.group(1), m.group(2)
            col = col_re.search(body)
            if not col:
                continue
            seq = f"{tname}_{col.group(1)}_seq"
            assert seq in text, f"{name}: {seq} no GRANT USAGE,SELECT"
