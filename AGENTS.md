# AGENTS.md — prime-jennie-runtime

> 이 파일은 Claude Code 및 병렬 작업자가 이 리포에서 작업할 때 따르는 규칙입니다.

## 리포 경계

- **이 리포**: v3 paper alpha 탐색 엔진. 느린 루프 + 빠른 루프 + v2 포팅 서비스. paper 로 alpha 를 증명하는 실험실이며 스스로 실계좌 자산을 매매하지 않는다. 실계좌 매매는 운영자의 텔레그램 지시·수락으로만 실행된다 (정체성: `.ai/designs/2026-05-29-paper-mode-alpha-discovery.md` + 2026-06-12 수정 `.ai/designs/2026-06-12-human-approved-trading-nl-interface.md`)
- **minyoung-mah**: Multi-Agent Harness 라이브러리. `pip install -e ../minyoung-mah`로 소비. **직접 수정하지 않음** (별도 repo, 별도 계정)
- **prime-jennie (v2)**: 참조 전용. 포팅 시 원본 경로 명시

## 폐기된 비전

향후 이 항목들로 회귀하지 않는다. 회귀 충동이 들면 이 표와 `.ai/designs/2026-05-29-paper-mode-alpha-discovery.md` §3 을 먼저 읽는다.

| 폐기 비전 | 폐기 시점 | 사유 |
|---|---|---|
| LLM-at-core (매 scout 코드 LLM 생성) | 5-22 | 138 run 전부 distinct code_hash, Jaccard 0.317 진동. 132건 -2.24% |
| multi-agent council debate | 4월 | 단일 LLM structured output 으로 단순화 |
| 실계좌 alpha 자동매매 | 5-29 | 자산 정체. paper 증명 후 재고 |
| 은퇴 후 방치 자율 운용 | 5-29 | 현 LLM 한계 (stateless·확률적·uncalibrated) 로 도달 불가 |

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
- `docs/MACRO_GATE_SPEC.md`
- `position_sheet/schema.py`

## 코드 규칙

- **언어**: 한국어 대화, 영어 코드, 한국어 주석 (v2 스타일 유지)
- **커밋**: Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`)
- **테스트**: 코드 변경 시 관련 테스트 필수. 테스트 없는 커밋 금지
- **린트**: 커밋 전 `ruff format . && ruff check .`
- **시크릿**: `.env`, 토큰, 키 절대 커밋 금지

## 결정 변경 룰

새 결정/설계를 commit 할 때는 그 결정이 무효화하는 자산을 같은 commit 또는 후속 commit 으로 함께 처리한다. 결정만 바꾸고 깨진 문서·코드를 방치하면 옛 문서가 다시 ground truth 행세를 한다.

- 깨진 docs → `docs/archive/` 로 이동
- 폐기된 `.ai/designs`·`.ai/decisions` → `.ai/archive/` 로 이동
- dead code → 같은 또는 후속 commit 으로 삭제
- 폐기된 방향이면 위 "폐기된 비전" 표에 한 줄 추가

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
  - `~/.claude/global-memory-youngs7596/communication-style.md` — 사용자와의 글쓰기 가이드 (아래 "글쓰기 가이드" 섹션과 동일 출처)
- **세션 시작 시:** **`gh auth switch -u youngs7596`** → `cd ~/.claude/global-memory-youngs7596 && git pull --rebase -q` → 위 파일들 적용. (auth switch 누락 시 git이 youngs75 토큰을 써서 private repo가 404로 보임)
- **장기 기억 승격:** 프로젝트 로컬 auto memory는 scratch 전용. 다른 머신에서도 필요한 지식은 위 파일로 승격
- **세션 종료 또는 `sync memory` 지시 시:** `gh auth switch -u youngs7596` → `cd ~/.claude/global-memory-youngs7596 && git add -A && git commit -m "..." && git push`
- **메모리 작성 톤:** 아래 "글쓰기 가이드" 를 따른다. 사람이 다시 읽을 글이라는 점을 잊지 말 것
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
- 글의 톤은 아래 "글쓰기 가이드" 를 따른다. 다음 세션의 사람과 LLM 둘 다 다시 읽는다는 점을 의식한다.

## 글쓰기 가이드 (세션·메모리·문서 공통)

세션 정리 파일, 글로벌 메모리, design doc, 분석 doc 등 사람이 다시 읽을 가능성이 있는 모든 글은 같은 톤으로 쓴다. 원본은 글로벌 메모리의 `communication-style.md` 이고, 여기서는 핵심만 옮긴다.

### 원칙 다섯 가지

1. **정직하게 말한다.** 근거 없이 추정하지 않는다. 모르면 모른다고 한다. 작업 시간을 일부러 길게 잡거나 단계를 잘게 쪼개서 복잡해 보이게 만들지 않는다.
2. **돌려 말하지 않는다.** "가능성이 있다", "어쩌면", "할 수도 있다" 같은 완충 표현을 남발하지 않는다. 결론은 단언하고, 확신이 없으면 그 사실 자체를 분명하게 말한다.
3. **사용자는 코드를 보고 있지 않다.** 파일·함수·변수 이름을 본문에 그대로 박지 않는다. 코드의 역할을 한국어로 풀어 쓴다. 근거를 댈 때는 파일 경로를 한두 줄 덧붙이는 정도로만 쓴다.
4. **결론은 평문 두세 문장으로 끝낸다.** 표·불릿·화살표·코드 블록은 근거를 보일 때까지만 쓴다. 다음 행동과 선택지는 일반 문장으로 짧게 마무리한다.
5. **LLM 이 자동으로 만드는 표현을 피한다 (2026-05-23 추가).** 다음 표현들은 영어·추상화에 익숙한 LLM 이 한국어로 옮길 때 자동으로 나오는데, 사람이 읽으면 거리감이 있다.

   - "본 분석", "본 세션", "본 design" → "이번 분석", "이 세션", "이 설계" 또는 그냥 생략
   - "정합", "정합성" → "맞는다", "일치한다", "같은 방향"
   - "Veto", "advisory", "scope", "single key", "trigger" 같은 영어 그대로 → "거부", "참고용", "범위", "핵심", "방아쇠/계기" 같은 한국어
   - "→" 화살표로 결론 잇기 → 문장으로 풀어 쓰기
   - "본질적", "치명적", "결정적" 같은 강조어 남발 → 정말 그럴 때만 쓰기
   - "Layer A / Stage 1 / Arm B" 같은 추상 라벨만 던지기 → 라벨 옆에 한 줄 설명 붙이기
   - "snapshot only", "shadow only", "fact layer" 처럼 영어와 한국어를 단어 단위로 섞기 → 통째로 한국어 또는 통째로 영어
   - "X 패턴", "Y 가설", "Z 메커니즘" 같은 추상명사구 남발 → 구체적인 동사로 풀어 쓰기
   - 결론에 또 표를 만들기 → 두세 문장 평문으로

### 판단 기준 한 줄

글을 다 쓴 뒤 한 번 더 읽어 본다. 사람이 사람에게 설명하는 말투인지 확인한다. 학술 논문 같거나 영어 직역 같으면 다시 쓴다.

### 적용 범위

- **세션 정리 파일** (`.ai/sessions/`): 본문 전체에 적용. 다음 세션의 사람과 LLM 모두 다시 읽는 글이다.
- **글로벌 메모리** (`~/.claude/global-memory-youngs7596/`): 본문 전체에 적용. 여러 머신에서 다시 읽힌다.
- **로컬 메모리** (`~/.claude/projects/.../memory/`): 본문 전체에 적용.
- **design doc / 분석 doc** (`.ai/designs/`, `.ai/analyses/`): 본문 전체에 적용. 단 분석 본문에서 근거를 보일 때는 표·코드 인용 사용해도 좋다. 결론·다음 행동 부분만 평문으로.
- **대화 응답**: 전부 적용. 특히 결론과 선택지 부분은 평문 두세 문장으로.

### 적용 시점

2026-05-23 부터. 그 이전에 작성된 문서들 (글로벌 메모리, 세션 파일, design doc 등) 은 사용자가 명시적으로 정리를 요청할 때까지 그대로 둔다. 이후 새로 쓰거나 보강하는 부분은 본 가이드에 맞춘다.

## 서브폴더 AGENTS.md

각 서브 패키지에 해당 디렉토리의 책임과 규칙을 기술하는 `AGENTS.md`가 있습니다. LLM이 디렉토리에 진입할 때 먼저 읽어야 합니다.
