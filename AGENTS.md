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

사용자는 여러 머신에서 작업하므로, Claude의 장기 기억은 **별도 private GitHub repo**(`~/.claude/global-memory-youngs7596/`, GitHub: `youngs7596/claude-global-memory`)로 동기화됩니다.

- **본 프로젝트가 참조할 글로벌 메모리 파일:**
  - `~/.claude/global-memory-youngs7596/prime-jennie-family.md` — 4 repo 구조, 핵심 설계 결정, v2 운영 상태
  - `~/.claude/global-memory-youngs7596/trading-domain.md` — 거래비용, KIS API, Exit Rules, Risk Throttle
  - `~/.claude/global-memory-youngs7596/minyoung-mah.md` — harness 라이브러리 궤적, gap feedback
- **세션 시작 시:** 위 파일들을 읽고 (`cd ~/.claude/global-memory-youngs7596 && git pull --rebase -q` 선행) 적용
- **장기 기억 승격:** 프로젝트 로컬 auto memory는 scratch 전용. 다른 머신에서도 필요한 지식은 위 파일로 승격
- **세션 종료 또는 `sync memory` 지시 시:** `cd ~/.claude/global-memory-youngs7596 && git add -A && git commit -m "..." && git push`
- **금지:** 프로젝트 로컬 auto memory에만 중요한 장기 기억을 남기지 말 것

## 서브폴더 AGENTS.md

각 서브 패키지에 해당 디렉토리의 책임과 규칙을 기술하는 `AGENTS.md`가 있습니다. LLM이 디렉토리에 진입할 때 먼저 읽어야 합니다.
