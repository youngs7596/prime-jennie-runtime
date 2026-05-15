"""Regression — executions.metadata_json 의 키 contract 검증.

2026-05-15 사고: persistence.py:143 의 record_sell 은 ``'exit_reason'`` 키로
저장하는데, 같은 세션에 도입된 cooldown 가드 5종이 모두 ``'reason'`` 키를
SELECT 하고 있어 항상 false negative. 운영 1일 만에 발견.

본 테스트는 writer (persistence) 와 reader (가드 SQL 5종) 가 같은 키를
참조하는지 정적으로 검증. drift 시 즉시 fail — prod 배포 전에 잡힌다.
"""

from __future__ import annotations

import inspect

EXPECTED_KEY = "exit_reason"


def test_persistence_writes_exit_reason_key():
    """fast_loop/persistence.py 의 record_sell 이 'exit_reason' 키를 쓰는지."""
    from prime_jennie_runtime.fast_loop import persistence

    src = inspect.getsource(persistence.PostgresTradeRecorder.record_sell)
    assert f'"{EXPECTED_KEY}":' in src, (
        f"record_sell 가 metadata 에 '{EXPECTED_KEY}' 키로 저장해야 함. "
        "키 변경 시 본 contract 테스트 + 가드 SQL 5종 동시 갱신 필수."
    )


def test_fast_loop_cooldown_check_reads_exit_reason():
    from prime_jennie_runtime.fast_loop import cooldown_check

    sql = cooldown_check.PgCooldownChecker._SQL
    assert f"metadata_json->>'{EXPECTED_KEY}'" in sql
    # 잘못된 키가 남아있지 않은지 확인 (별칭은 제외 — 'AS reason' 은 OK).
    assert "metadata_json->>'reason'" not in sql, (
        "잘못된 키 'reason' 이 SQL 에 남아있음 — drift 검출"
    )


def test_coordinator_policy_reads_exit_reason():
    from prime_jennie_runtime.coordinator.policies import recent_stoploss_cooldown

    sql = recent_stoploss_cooldown._SQL
    assert f"metadata_json->>'{EXPECTED_KEY}'" in sql
    assert "metadata_json->>'reason'" not in sql


def test_pg_active_sheet_checker_reads_exit_reason():
    from prime_jennie_runtime.slow_loop.strategy import engine

    assert f"metadata_json->>'{EXPECTED_KEY}'" in engine._SQL_HAS_RECENT_STOP
    assert "metadata_json->>'reason'" not in engine._SQL_HAS_RECENT_STOP

    src = inspect.getsource(engine.PgActiveSheetChecker.has_recent_stop_loss)
    assert f"metadata_json->>'{EXPECTED_KEY}'" in src
    assert "metadata_json->>'reason'" not in src


def test_scout_context_builder_reads_exit_reason():
    from prime_jennie_runtime.slow_loop.scout import context_builder

    sql = context_builder._SQL_RECENT_STOPS_SA
    assert f"metadata_json->>'{EXPECTED_KEY}'" in sql
    assert "metadata_json->>'reason'" not in sql
