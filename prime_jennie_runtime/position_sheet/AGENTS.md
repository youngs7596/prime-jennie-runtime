# `position_sheet/` — 포지션 시트 스키마 (공유 계약)

Track A에서 생성, 모든 Track이 소비. **수정 시 stop-the-world.**

## 파일

- `schema.py` — PositionSheet Pydantic v1.1 전수 구현

## 정본 문서

`docs/POSITION_SHEET_SPEC.md` — 변경 시 이 문서와 코드를 동시에 갱신

## 규칙

- `schema.py` 변경은 모든 Track 담당자에게 사전 공지
- MAJOR 버전 변경(필드 삭제/의미 변경)은 Executor 업그레이드 선행 필수
- MINOR 변경(필드 추가, 새 enum)은 하위 호환 유지
- meta가 이 파일을 수정하는 PR은 자동 거부
