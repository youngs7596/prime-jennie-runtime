"""Scout role 단발 호출 헬퍼.

``code_loop.generate_validated_code`` 와 ``scripts/scout_replay`` 둘 다 쓰는 공통
호출 진입점. minyoung-mah ``Orchestrator`` 위에 ``ScoutContext`` 를 invocation
metadata 에 실어 호출하는 패턴을 한 곳으로.

기존 ``slow_loop/pipeline.py`` 의 ``_scout_pipeline`` + ``_run_pipeline_with_retry``
를 그대로 활용 — 검증 루프가 별도 retry 를 갖는 것과 호환되도록 단발 한 회만 호출.
"""

from __future__ import annotations

from typing import Any

from minyoung_mah import Observer, Orchestrator

from .schemas import ScoutContext


async def run_scout_once(
    orch: Orchestrator,
    scout_ctx: ScoutContext,
    observer: Observer,
) -> Any:
    """Scout role 한 번 호출. 결과 PipelineStepResult (state["scout"]) 반환."""
    # 순환 import 회피: pipeline.py 가 schemas/code_loop 를 끌어다 쓰므로 함수 안에서 import
    from prime_jennie_runtime.slow_loop.pipeline import (
        _run_pipeline_with_retry,
        _scout_pipeline,
    )

    res = await _run_pipeline_with_retry(
        orch,
        _scout_pipeline(scout_ctx),
        observer,
        role_name="scout",
        user_request="daily scout run",
    )
    return res.state["scout"]
