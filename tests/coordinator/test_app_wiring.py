"""Coordinator listener daemon entrypoint wiring smoke.

app.py 는 단순한 redis/asyncpg + run_listener wiring 이라 실 동작은 listener
integration test 가 커버한다. 본 테스트는 모듈 import + SERVICE_NAME 상수 +
run() 정의 존재 정도만 확인 (regression: import 시점 crash 방지).
"""

from __future__ import annotations

import inspect


def test_app_module_imports() -> None:
    from prime_jennie_runtime.coordinator import app

    assert app.SERVICE_NAME == "coordinator-listener"
    assert inspect.iscoroutinefunction(app.run)
