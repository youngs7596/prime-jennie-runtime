"""ScreeningToolAdapter — 호스트 측 어댑터 (SCOUT §6.5).

Scout가 생성한 Python 코드를 격리된 subprocess(같은 파이썬 인터프리터)에서
실행하고 list[ScreeningCandidate]를 반환. ScreeningToolAdapterStub과 시그니처 호환:

    async def invoke(code: str, context: dict) -> list[ScreeningCandidate]

backend (2026-05-11 정리):
- 과거 ``docker`` backend 가 선언만 있었고 실제로는 subprocess 만 호출했다
  (compose service `screening-executor` 도 `build-only` 프로파일).
- Phase 2.10 #11 정리에서 docker 분기 제거 → ``subprocess`` 단일 백엔드.
  운영 격리는 slow-loop 컨테이너의 host network 격리 + executor.py AST
  화이트리스트로 확보. 향후 진짜 컨테이너 격리가 필요해지면 다시 도입.

ToolAdapter (call/arg_schema) 프로토콜 conformance는 Phase 1 에서 미사용.
slow_loop.pipeline 이 직접 invoke()를 호출하므로 그 시그니처에 맞춘다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any

from prime_jennie_runtime.slow_loop.scout.schemas import ScreeningCandidate

from .schemas import ScreeningResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300.0  # SCOUT §4.2 #6
EXECUTOR_MODULE = "prime_jennie_runtime.screening_executor.executor"


@dataclass
class ScreeningToolAdapter:
    """Scout 코드를 같은 인터프리터의 별도 프로세스에서 실행하는 어댑터.

    stdin 으로 JSON payload 를 주고 stdout 마지막 줄에서 ``ScreeningResult``
    JSON 을 파싱한다.
    """

    timeout_s: float = DEFAULT_TIMEOUT_S

    async def invoke(
        self,
        code: str,
        context: dict[str, Any] | None = None,
    ) -> list[ScreeningCandidate]:
        """ScreeningToolAdapterStub과 동일한 시그니처. 실패 시 빈 list 반환."""
        result = await self.run(code, context or {})
        if not result.ok:
            logger.warning("screening failed: error=%s details=%s", result.error, result.details)
            return []
        return result.candidates

    async def run(
        self,
        code: str,
        context: dict[str, Any],
        market_data: Any | None = None,
    ) -> ScreeningResult:
        """invoke의 raw 형태 — ScreeningResult 그대로 반환 (테스트/observer용)."""
        payload = json.dumps(
            {"code": code, "context": context, "market_data": market_data}
        ).encode()

        argv = self._build_argv()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            return ScreeningResult(
                ok=False,
                error="subprocess_error",
                details=f"executor binary not found: {e}",
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload), timeout=self.timeout_s
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ScreeningResult(ok=False, error="timeout", details=None)

        if proc.returncode != 0:
            return ScreeningResult(
                ok=False,
                error="subprocess_error",
                details=(stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"),
            )

        return self._parse_stdout(stdout)

    # ----- internals -----

    def _build_argv(self) -> list[str]:
        return [sys.executable, "-m", EXECUTOR_MODULE]

    def _parse_stdout(self, stdout: bytes) -> ScreeningResult:
        text = stdout.decode(errors="replace").strip()
        if not text:
            return ScreeningResult(ok=False, error="subprocess_error", details="empty stdout")
        # 마지막 비어있지 않은 줄이 결과 JSON
        last_line = text.splitlines()[-1]
        try:
            return ScreeningResult.model_validate_json(last_line)
        except Exception as e:
            return ScreeningResult(
                ok=False,
                error="subprocess_error",
                details=f"stdout parse failed: {type(e).__name__}: {e}",
            )
