"""Macro Council 로깅/히스토리/리플레이/리포트 레이어.

v2 `prime_jennie/services/council/` 의 데이터/조회 계층을 v3 로 포팅.
3-step LLM 파이프라인 자체는 v3 `slow_loop/macro/` 가 담당 — 여기서는 실행 결과의
영속/조회/포매팅만 책임.
"""

from .persistence import (
    fetch_council_run,
    list_council_runs,
    record_as_dict,
    save_council_run,
)
from .records import CouncilRunRecord, CouncilStepOutput
from .replay import CouncilReplay, build_replay, replay_council_run
from .report import format_council_run_html, format_council_run_text

__all__ = [
    "CouncilReplay",
    "CouncilRunRecord",
    "CouncilStepOutput",
    "build_replay",
    "fetch_council_run",
    "format_council_run_html",
    "format_council_run_text",
    "list_council_runs",
    "record_as_dict",
    "replay_council_run",
    "save_council_run",
]
