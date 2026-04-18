# AGENTS.md — prime-jennie-runtime

> 이 파일은 Claude Code 및 병렬 작업자가 이 리포에서 작업할 때 따르는 규칙입니다.

## 리포 경계

- **이 리포**: v3 실행 엔진. 느린 루프 + 빠른 루프 + v2 포팅 서비스
- **minyoung-mah**: Multi-Agent Harness 라이브러리. `pip install -e ../minyoung-mah`로 소비. **직접 수정하지 않음** (별도 repo, 별도 계정)
- **prime-jennie (v2)**: 참조 전용. 포팅 시 원본 경로 명시

## Track 소유권 (Phase 1)

| Track | 소유 디렉토리 | 건드리면 안 되는 곳 |
|-------|--------------|-------------------|
| A (인프라) | `infra/`, `migrations/` | 다른 Track 디렉토리 |
| B (느린 루프) | `slow_loop/`, `position_sheet/` | `infra/` (read only) |
| C (빠른 루프) | `fast_loop/`, `kis_gateway/`, `telegram_bot/` | 다른 Track |
| D (Screening) | `screening_executor/` | B의 schema import만 |
| E (데이터/뉴스) | `news_pipeline_kor/`, `migrations/` | A와 migrations/ 번호 조율 |

## 공유 스펙 (변경 시 stop-the-world)

- `docs/POSITION_SHEET_SPEC.md`
- `docs/SCOUT_CODE_GENERATION.md`
- `docs/MACRO_GATE_SPEC.md`
- `position_sheet/schema.py`

## 코드 규칙

- **언어**: 한국어 대화, 영어 코드, 한국어 주석 (v2 스타일 유지)
- **커밋**: Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`)
- **테스트**: 코드 변경 시 관련 테스트 필수. 테스트 없는 커밋 금지
- **린트**: 커밋 전 `ruff format . && ruff check .`
- **시크릿**: `.env`, 토큰, 키 절대 커밋 금지

## minyoung-mah 소비 규칙

- `pip install -e ../minyoung-mah` (editable)
- Harness 내부 private API 직접 import 금지
- Prime Jennie 전용 필드를 harness에 요청하지 않음 (consumer extension으로 해결)
- harness 수정 필요 시: **`gh auth switch -u youngs75`** 후 별도 PR

## v2 포팅 규칙 (Track C, E)

- 원본 v2 경로를 커밋 메시지에 명시
- 재작성 금지, 포팅 후 v3 인터페이스 어댑터만 추가
- v2에서 안정적이었던 부분을 다시 짜는 건 자존심 낭비 (설계 원칙 1.6)

## Memory Sync

사용자는 여러 머신에서 작업하므로, Claude의 장기 기억은 **별도 private GitHub repo**(`~/.claude/global-memory-youngs7596/`, GitHub: `youngs7596/claude-global-memory`)로 동기화됩니다. 본 프로젝트(prime-jennie family)는 youngs7596 계정 메모리만 참조합니다. 사내 교육 프로젝트(apt-family 등)는 별도 `youngs75/claude-global-memory`에 분리 저장 — 교차 저장 금지.

- **본 프로젝트가 참조할 글로벌 메모리 파일:**
  - `~/.claude/global-memory-youngs7596/prime-jennie-family.md` — 4 repo 구조, 핵심 설계 결정, v2 운영 상태
  - `~/.claude/global-memory-youngs7596/trading-domain.md` — 거래비용, KIS API, Exit Rules, Risk Throttle
  - `~/.claude/global-memory-youngs7596/minyoung-mah.md` — harness 라이브러리 궤적, gap feedback
- **세션 시작 시:** **`gh auth switch -u youngs7596`** → `cd ~/.claude/global-memory-youngs7596 && git pull --rebase -q` → 위 파일들 적용. (auth switch 누락 시 git이 youngs75 토큰을 써서 private repo가 404로 보임)
- **장기 기억 승격:** 프로젝트 로컬 auto memory는 scratch 전용. 다른 머신에서도 필요한 지식은 위 파일로 승격
- **세션 종료 또는 `sync memory` 지시 시:** `gh auth switch -u youngs7596` → `cd ~/.claude/global-memory-youngs7596 && git add -A && git commit -m "..." && git push`
- **금지:** 프로젝트 로컬 auto memory에만 중요한 장기 기억을 남기지 말 것
- **금지:** 사내 교육 프로젝트(apt-family 등) 관련 메모리를 youngs7596 repo에 쓰지 말 것 (반대 방향도 동일)

## 세션 파일 명명 규칙
세션 기록은 `.ai/sessions/session-YYYY-MM-DD-NNNN.md` 형식을 사용한다.

- `YYYY-MM-DD`: 세션 당일 날짜
- `NNNN`: 같은 날짜 내 순번 (`0001`부터 시작)
- 같은 날짜 파일이 있으면 가장 큰 번호에 `+1`을 적용한다.

## Resume 규칙
사용자가 `resume` 또는 `이어서`라고 요청하면 가장 최근 세션 파일을 찾아 이어서 작업한다.

- `.ai/sessions/`에서 명명 규칙에 맞는 파일만 후보로 본다.
- 가장 최신 날짜를 우선, 같은 날짜면 가장 큰 순번을 선택한다.
- 선택한 세션 파일은 전체를 읽고, 사용자에게 이전 작업 내용과 다음 할 일을 한국어로 간단히 브리핑한다.

## Handoff 규칙
새 세션 파일은 사용자가 명시적으로 종료를 요청한 경우에만 생성한다.
허용 트리거 예: `handoff`, `정리해줘`, `세션 저장`, `종료하자`, `세션 종료`.

- 저장 위치는 항상 `.ai/sessions/`.
- 기존 `session-*.md` 파일은 절대 수정하지 않는다.
- 자동/단계별 저장은 하지 않는다.
- 새 파일에는 프로젝트 개요, 최근 작업 내역, 현재 상태, 다음 단계, 중요 참고사항을 포함한다.

## 서브폴더 AGENTS.md

각 서브 패키지에 해당 디렉토리의 책임과 규칙을 기술하는 `AGENTS.md`가 있습니다. LLM이 디렉토리에 진입할 때 먼저 읽어야 합니다.
