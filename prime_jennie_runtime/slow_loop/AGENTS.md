# `slow_loop/` — 느린 루프 (LLM 파이프라인)

Track B 소유. minyoung-mah `StaticPipeline`으로 오케스트레이션.

## 서브모듈

| 디렉토리 | 역할 | minyoung-mah 프로토콜 |
|---|---|---|
| `scout/` | 스크리닝 Python 코드 생성 | `SubAgentRole` (structured fast path) |
| `macro/` | 바이너리 게이트 + size_multiplier | `SubAgentRole` (structured fast path) |
| `strategy/` | 결정론적 룰엔진 → PositionSheet 발행 | 프로토콜 미사용 (LLM 없음) |

## 파이프라인 흐름

```
Scout (STRONG tier) → ScoutOutput
  ↓
Screening Executor (ToolAdapter, 격리 컨테이너)
  ↓
Macro Gate (REASONING tier) → MacroGateOutput
  ↓
Strategy Engine (결정론) → PositionSheet → Redis Stream
```

## 핵심 규칙

- `scout/`: 코드를 생성하지, 종목을 추천하지 않는다 (SCOUT_CODE_GENERATION.md)
- `macro/`: reasoning은 로깅 전용, 실행 로직 참조 절대 금지 (MACRO_GATE_SPEC.md)
- `strategy/`: LLM 호출 금지. 모든 결정은 결정론적 코드
- 공유 스펙 변경 시 stop-the-world
