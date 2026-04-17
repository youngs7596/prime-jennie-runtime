"""격리 환경 안에서 Scout 코드를 실행하는 in-process executor (SCOUT §6.4).

동일 코드가 두 위치에서 쓰인다:
1. 호스트 측 단위테스트: ScreeningExecutor 인스턴스로 직접 호출
2. 컨테이너 내부: `python -m prime_jennie_runtime.screening_executor.executor`로
   stdin → stdout JSON 프로토콜로 호출 (Track D adapter가 docker run으로 spawn)

검증/dedup/cap의 일부 책임은 slow_loop.scout.validators가 또 한 번 한다.
executor에서는 §6.4의 "21+ → 상위 20개" 까지만 가벼운 cap을 둔다 (이중 안전장치).
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from prime_jennie_runtime.slow_loop.scout.schemas import ScreeningCandidate

from .allowlist import check_imports
from .schemas import ScreeningResult

CANDIDATE_HARD_CAP = 20  # SCOUT §4.2 #3, §6.4


@dataclass
class ScreeningExecutor:
    """단일 Scout 코드를 in-process에서 실행.

    명세 §6.4의 동작을 충실히 따르되 in-process이므로 timeout/메모리 제한은
    구현하지 않음 (그건 격리 컨테이너 + ScreeningToolAdapter의 책임).
    """

    candidate_cap: int = CANDIDATE_HARD_CAP

    def run(
        self,
        code: str,
        context: dict[str, Any],
        market_data: Any | None = None,
    ) -> ScreeningResult:
        """Scout 코드를 검사·실행해 ScreeningResult로 환원.

        market_data는 SCOUT §2.3대로 컨테이너 마운트에서 로드하는 것이 정설이지만
        Phase 1 Track D는 호출자가 인자로 주입하는 형태로 단순화.
        """
        t0 = time.perf_counter()

        violations = check_imports(code)
        if violations:
            error: str
            if any(v.startswith("call:") for v in violations):
                error = "forbidden_call"
            else:
                error = "import_violation"
            return ScreeningResult(
                ok=False,
                error=error,  # type: ignore[arg-type]
                details=violations,
                exec_time_ms=_ms(t0),
            )

        namespace: dict[str, Any] = {}
        try:
            compiled = compile(code, "<scout_code>", "exec")
        except SyntaxError as e:
            return ScreeningResult(
                ok=False,
                error="compile_error",
                details=f"{type(e).__name__}: {e.msg} (line {e.lineno})",
                exec_time_ms=_ms(t0),
            )

        try:
            exec(compiled, namespace)  # noqa: S102 — allowlist 통과 후 의도적 exec
        except Exception as e:
            return ScreeningResult(
                ok=False,
                error="compile_error",
                details=f"{type(e).__name__}: {e}",
                exec_time_ms=_ms(t0),
            )

        screen_fn = namespace.get("screen")
        if not isinstance(screen_fn, Callable):  # type: ignore[arg-type]
            return ScreeningResult(
                ok=False,
                error="screen_not_defined",
                details="`screen` callable not found in compiled namespace",
                exec_time_ms=_ms(t0),
            )

        try:
            raw = screen_fn(market_data, context)
        except Exception as e:
            return ScreeningResult(
                ok=False,
                error="runtime_error",
                details=f"{type(e).__name__}: {e}",
                exec_time_ms=_ms(t0),
            )

        if not isinstance(raw, list):
            return ScreeningResult(
                ok=False,
                error="invalid_return_type",
                details=f"expected list, got {type(raw).__name__}",
                exec_time_ms=_ms(t0),
            )

        candidates: list[ScreeningCandidate] = []
        for idx, item in enumerate(raw):
            try:
                if isinstance(item, ScreeningCandidate):
                    candidates.append(item)
                elif isinstance(item, dict):
                    candidates.append(ScreeningCandidate.model_validate(item))
                else:
                    return ScreeningResult(
                        ok=False,
                        error="invalid_return_type",
                        details=f"item[{idx}] is {type(item).__name__}, not dict/Candidate",
                        exec_time_ms=_ms(t0),
                    )
            except ValidationError as e:
                return ScreeningResult(
                    ok=False,
                    error="invalid_return_type",
                    details=f"item[{idx}] schema mismatch: {e.errors()[0]['msg']}",
                    exec_time_ms=_ms(t0),
                )

        truncated = False
        if len(candidates) > self.candidate_cap:
            candidates = candidates[: self.candidate_cap]
            truncated = True

        return ScreeningResult(
            ok=True,
            candidates=candidates,
            exec_time_ms=_ms(t0),
            truncated=truncated,
        )


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


# ---------------------------------------------------------------------
# 컨테이너 내부 진입점: stdin payload → stdout result (json)
#   payload = {"code": str, "context": dict, "market_data": <pickled?>}
#   Phase 1: market_data는 None 또는 dict (간단 직렬화 가능한 형태) 만 지원.
# ---------------------------------------------------------------------


def _stdio_main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": "subprocess_error", "details": str(e)}))
        return 1

    code = payload.get("code", "")
    context = payload.get("context", {}) or {}
    market_data = payload.get("market_data")  # 호출자 책임

    executor = ScreeningExecutor()
    result = executor.run(code, context, market_data=market_data)
    print(result.model_dump_json())
    return 0 if result.ok else 0  # exit 0 always — adapter parses stdout


if __name__ == "__main__":
    raise SystemExit(_stdio_main())
