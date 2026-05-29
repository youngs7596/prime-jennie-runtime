# prompts/ — LLM 프롬프트 자산

v2 `prime-jennie/prompts/` 에서 포팅. 판정/이관 기록은
`docs/archive/PHASE_2_10_UTILITIES_INVENTORY.md` 참조 (2026-05-29 archive 이동).

## 디렉토리

- `briefing/` — 일일 브리핑 (#6 briefing 서비스 소비)
- `council/` — 매크로 council 3역할 프롬프트 (#7 council 로깅 소비)

## 원칙

- **원형 유지**: v2 에서 검증된 프롬프트는 재작성 금지 (AGENTS.md §v2 포팅 규칙)
- **소유권**: 각 서비스 Track 이 자기 소비 프롬프트를 이 디렉토리에서 **read-only** 로 로드
- **새 프롬프트**: 해당 서비스 디렉토리 (`prime_jennie_runtime/<service>/prompts.py`) 에 인라인
  — 서비스 로직과 타이트하게 결합되는 프롬프트는 서비스 코드에 둔다 (slow_loop/scout/prompts.py 처럼)
