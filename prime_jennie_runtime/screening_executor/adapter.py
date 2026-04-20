"""ScreeningToolAdapter — 호스트 측 어댑터 (SCOUT §6.5).

Scout가 생성한 Python 코드를 격리 컨테이너 (또는 단순 subprocess)에서 실행하고
list[ScreeningCandidate]를 반환. ScreeningToolAdapterStub과 시그니처 호환:

    async def invoke(code: str, context: dict) -> list[ScreeningCandidate]

두 가지 backend:
- "docker": `docker run --rm -i --network=none ...` 격리 컨테이너 spawn (운영)
- "subprocess": 같은 파이썬 인터프리터를 별도 프로세스로 실행 (dev/CI, 컨테이너 미가용)

ToolAdapter 프로토콜(call/arg_schema)은 Phase 1에서 미사용. slow_loop.pipeline이
직접 invoke()를 호출하므로 그 시그니처에 맞춘다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

from prime_jennie_runtime.slow_loop.scout.schemas import ScreeningCandidate

from .schemas import ScreeningResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300.0  # SCOUT §4.2 #6
DEFAULT_IMAGE = "ghcr.io/youngs7596/prime-jennie-runtime/screening-executor:latest"
EXECUTOR_MODULE = "prime_jennie_runtime.screening_executor.executor"

# 호스트 절대경로로 seccomp profile 을 주입하면 docker run 시 --security-opt=seccomp 추가.
# slow-loop 컨테이너가 docker-in-docker 구조로 호스트 데몬에 명령하므로 파일 경로는
# **호스트 기준**이어야 한다 (slow-loop 컨테이너 내부 경로 ❌).
SECCOMP_ENV_KEY = "SCREENING_SECCOMP_PROFILE"


@dataclass
class ScreeningToolAdapter:
    """Scout 코드를 격리 프로세스에서 실행하는 어댑터.

    Phase 1: docker / subprocess 두 backend. 둘 다 stdin으로 JSON payload를 주고
    stdout 마지막 줄에서 ScreeningResult JSON을 파싱한다.
    """

    backend: Literal["docker", "subprocess"] = "subprocess"
    timeout_s: float = DEFAULT_TIMEOUT_S
    image: str = DEFAULT_IMAGE
    docker_extra_args: list[str] = field(default_factory=list)

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
        if self.backend == "docker":
            argv: list[str] = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network=none",
                "--read-only",
                "--memory=4g",
                "--cpus=2",
                "--security-opt=no-new-privileges:true",
                "--cap-drop=ALL",
                "--user=1000:1000",
                "--tmpfs=/tmp:size=256m",
            ]
            seccomp_profile = os.environ.get(SECCOMP_ENV_KEY, "").strip()
            if seccomp_profile:
                argv.append(f"--security-opt=seccomp={seccomp_profile}")
            argv.extend(self.docker_extra_args)
            argv.append(self.image)
            return argv
        # subprocess: same interpreter, executor module
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
