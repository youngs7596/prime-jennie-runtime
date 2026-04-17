# council_logging — Macro Council 로깅/히스토리/리플레이/리포트 레이어

**Track D Phase 2.10** — v2 `prime-jennie/prime_jennie/services/council/` 의 데이터 영속/조회/
리포트 계층을 v3 로 포팅. v2 의 3-step LLM 파이프라인 자체(`pipeline.py`)는 v3 의
`slow_loop/macro/` 가 단일-step `MacroGate` 로 이미 대체했으므로 **포팅 대상에서 제외**.

## 스코프

이 모듈은 아래만 담당:

1. **Record schema (`records.py`)** — Council 실행 1회의 입력/출력/비용/메타를 표현하는
   dataclass. v2 `CouncilInput`/`CouncilResult` 와 호환되는 필드를 포함하되, v3
   `MacroGate` 단일-step 결과도 같은 레코드에 담을 수 있도록 선택적 필드로 구성.
2. **Persistence (`persistence.py`)** — `macro_runs` 테이블 + `metadata_json` JSONB 에
   Council 원시 출력을 기록/조회. 신규 마이그레이션 없이 기존 스키마로 동작.
3. **Report (`report.py`)** — 저장된 레코드를 텔레그램/대시보드용 HTML/텍스트 로 포매팅.
4. **Replay (`replay.py`)** — 과거 Council 런을 레코드 형태로 복원 (입력/출력/비용 포함).

## 범위 밖 (Phase 3 또는 다른 트랙)

- **3-step pipeline 자체 (strategist → risk_analyst → chief_judge)** — v3 슬로우루프가
  단일-step Macro Gate 로 운영 중. 3-step 으로 회귀하려면 Phase 3 에서 별도 제안 필요.
- **Telegram 채널 수집 (`telegram_collector.py`)** — v3 `news_pipeline_kor/` 또는
  telegram_bot 측 책임. council_logging 은 이미 수집된 텍스트를 받아 저장만 함.
- **LLM 호출** — 호출은 slow_loop 이 담당. 이 모듈은 결과만 기록.
- **마이그레이션** — `macro_runs.metadata_json` JSONB 에 올라타서 신규 테이블 추가 금지
  (team-lead 지침).

## 포팅 원본

- `prime-jennie/prime_jennie/services/council/__init__.py`
- `prime-jennie/prime_jennie/services/council/pipeline.py` (CouncilInput/CouncilResult
  dataclass 구조만)
- `prime-jennie/prime_jennie/services/council/schemas.py` (JSON 스키마 — validation 용)
- `prime-jennie/prime_jennie/infra/database/models.py::DailyMacroInsightDB`
  (`raw_council_output_json`, `council_cost_usd`, `trading_reasoning`,
  `council_consensus` 등 로깅 필드 참고)

## 공개 API

```python
from prime_jennie_runtime.council_logging import (
    CouncilRunRecord,
    CouncilStepOutput,
    save_council_run,
    fetch_council_run,
    list_council_runs,
    format_council_run_text,
    format_council_run_html,
    replay_council_run,
)
```

## 연동 지점

- **slow_loop (Macro Gate)**: 각 실행 말미에 `save_council_run(engine, record)` 호출.
- **dashboard/macro router**: `list_council_runs` / `fetch_council_run` 로 히스토리 노출.
- **telegram_bot 일일 브리핑**: `format_council_run_text(record)` 로 요약 송출.
