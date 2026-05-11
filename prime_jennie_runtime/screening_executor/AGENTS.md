# `screening_executor/` — 격리 샌드박스 코드 실행

Track D 소유. Scout가 생성한 Python 코드를 안전하게 실행.

## 격리 수준

- 컨테이너: non-privileged, non-root, `network: none`
- 리소스: 4GB 메모리, 2 CPU, 300s 타임아웃
- 파일시스템: read-only + `/tmp` tmpfs
- seccomp profile 적용

## Import 화이트리스트

```
pandas, numpy, scipy.stats, talib,
sklearn.cluster, sklearn.linear_model, sklearn.preprocessing, sklearn.metrics,
math, statistics, datetime
```

**명시 금지**: os, sys, subprocess, socket, importlib, ctypes, eval, exec, compile

## minyoung-mah 연동

`ScreeningToolAdapter` — Scout의 `screening_code`를 받아 격리 프로세스(또는 컨테이너)에서 실행하고 `list[ScreeningCandidate]` 반환. ScreeningToolAdapterStub과 동일한 `async def invoke(code, context)` 시그니처를 노출하므로 slow_loop pipeline에 stub 자리를 그대로 대체 가능.

ToolAdapter (call/arg_schema) 프로토콜 conformance는 Phase 1 미충족 — slow_loop pipeline이 Orchestrator 경유 호출이 아니라 직접 invoke하므로 불필요. Phase 2에서 Orchestrator-driven scout 흐름이 생기면 그때 conformance 부여.

## 백엔드 (2026-05-11 정리)

`subprocess` 단일 백엔드. 같은 Python 인터프리터로 `prime_jennie_runtime.screening_executor.executor` 모듈을 별도 프로세스로 실행 → stdin JSON payload, stdout 마지막 줄에서 `ScreeningResult` 파싱.

과거에는 `backend="docker"` 분기가 있었지만 실제로는 docker spawn 가능 환경이 없어서 호출되지 않았고, slow-loop 컨테이너 + executor AST 화이트리스트로 격리가 충분하다고 판단해 제거. `SCREENING_BACKEND` env 도 unused — 컴포즈에서 빼도 무방.

이미지 `infra/docker/Dockerfile.screening` / `screening-executor` compose 서비스는 미래 격리 강화를 위해 `build-only` 프로파일로 유지 (현재 미사용).

## 필수 테스트

악의 코드 테스트 12건 이상 (M01~M12). 모두 거부되어야 함. AST 정적 거부(M01~M07,M11,M12)는 `tests/screening_executor/test_allowlist.py`. 런타임 거부(M08 timeout)는 `test_adapter.py::test_subprocess_timeout_kills_child`. M09 OOM / M10 fork bomb은 컨테이너 격리 e2e 영역 (Phase 1 미작성).
