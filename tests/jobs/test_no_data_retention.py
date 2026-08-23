"""수집한 시장 데이터를 지우는 코드가 없는지 지키는 회귀 테스트.

2026-08-23 결정: KIS 는 어떤 API 로도 3개월 이전 데이터를 주지 않는다. 그래서
수집 잡이 매일 쌓는 행은 한 번 지우면 복구할 수 없다. 옛 `cleanup_old_data`
(일봉 365일 삭제) 를 없앴고, 같은 것이 다시 들어오지 못하게 막는다.

앞의 테스트 파일 `test_maintenance.py` 는 그 잡의 cutoff 계산을 검증하던 것이라
잡과 함께 사라졌다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[2] / "prime_jennie_runtime"

# 행이 쌓이기만 해야 하는 테이블 — 여기서 DELETE 가 보이면 실패.
_ACCUMULATING_TABLES = (
    "daily_prices",
    "minute_prices",
    "realtime_ticks",
    "realtime_orderbook",
    "stock_investor_tradings",
    "stock_fundamentals",
    "futures_night_oi",
    "futures_oi_snapshots",
    "news_articles",
    "index_daily_prices",
)


def _python_sources() -> list[Path]:
    return [p for p in _PKG.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("table", _ACCUMULATING_TABLES)
def test_no_delete_against_accumulating_table(table: str) -> None:
    pattern = re.compile(rf"DELETE\s+FROM\s+{table}\b", re.IGNORECASE)
    offenders = [
        f"{path.relative_to(_PKG.parent)}:{i}"
        for path in _python_sources()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, (
        f"{table} 는 쌓기만 하는 테이블인데 삭제문이 있다: {offenders}. "
        "KIS 는 3개월 이전을 안 주므로 지우면 복구 불가 — jobs/AGENTS.md 참조."
    )


def test_cleanup_handler_is_gone() -> None:
    from prime_jennie_runtime.jobs import maintenance

    assert not hasattr(maintenance, "cleanup_old_data")
    assert not hasattr(maintenance, "DEFAULT_CLEANUP_DAYS")


def test_cleanup_job_not_seeded() -> None:
    from scripts.seed_scheduled_jobs import SEEDS

    assert not [j for j in SEEDS if "cleanup" in j.id or "cleanup" in j.handler_key]
