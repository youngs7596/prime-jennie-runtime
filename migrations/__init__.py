"""v2 → v3 데이터 마이그레이션 스크립트.

SQL: ``001_v3_schema.sql``, ``002_legacy_namespace.sql``.
Python CLI: ``migrate_signal_logs.py``, ``migrate_trade_logs.py``,
``migrate_daily_quant_scores.py``, ``verify_migration.py``.

Phase 0 design §13. 일방향 (v2 MariaDB → v3 Postgres).
"""
