# prime-jennie-runtime

Prime Jennie v3 실행 엔진. 자가진화 KOSPI/KOSDAQ 트레이딩 시스템의 핵심 런타임.

## 아키텍처

- **느린 루프**: Scout (코드 생성) → Screening Executor (격리 실행) → Macro Gate (바이너리 판정) → Strategy Engine → 포지션 시트
- **빠른 루프**: Executor (LLM 금지, 결정론 코드) → KIS Gateway → 증권사 실주문
- **인터페이스**: 포지션 시트 JSON (Redis Stream)

[minyoung-mah](https://github.com/youngs75/minyoung-mah) Multi-Agent Harness 라이브러리를 소비합니다.

## 설치

```bash
# 가상환경
uv venv && source .venv/bin/activate

# 의존성 설치
uv pip install -e ".[dev]"

# minyoung-mah editable 연결
uv pip install -e ../minyoung-mah
```

## 인프라 기동

```bash
docker compose up -d postgres redis
```

## 테스트

```bash
pytest tests/ -v
```

## 설계 문서

- `docs/POSITION_SHEET_SPEC.md` — 포지션 시트 JSON 전수 명세
- `docs/SCOUT_CODE_GENERATION.md` — Scout 코드 생성 명세
- `docs/MACRO_GATE_SPEC.md` — Macro Gate 명세

## 관련 리포

| Repo | 역할 |
|------|------|
| [minyoung-mah](https://github.com/youngs75/minyoung-mah) | Multi-Agent Harness 라이브러리 |
| prime-jennie-meta | 자가진화 엔진 (Phase 3) |
| prime-jennie-control-ui | 모니터링/제어 UI (Phase 2.5) |
| [prime-jennie](https://github.com/youngs7596/prime-jennie) | v2 (공존 운영 중) |
