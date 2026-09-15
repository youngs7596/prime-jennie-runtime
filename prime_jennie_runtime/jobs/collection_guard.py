"""수집 잡이 한 건도 못 건졌을 때 실패로 신고하게 하는 가드.

2026-09-10 네이버가 사이트를 옮겼을 때, 크롤러는 실패를 None 으로 흡수하고 잡은
카운트만 올린 뒤 정상 종료했다. 그래서 잡 실행 기록은 엿새 내내 success 였고
아무것도 안 들어오는 것을 밤 9 시 계약 스모크 검사 하나만 알고 있었다.

개별 종목이 몇 개 빠지는 것은 늘 있는 일이지만 **대상이 있는데 한 건도 못 건진
것은 출처가 죽었다는 뜻**이다. 그 둘을 갈라 뒤쪽만 예외로 올린다.
"""

from __future__ import annotations


class CollectionSourceDeadError(RuntimeError):
    """대상 종목이 있는데 한 건도 수집하지 못했다 — 출처가 죽은 것으로 본다."""


def raise_if_nothing_collected(job_name: str, *, collected: int, candidates: int) -> None:
    """한 건도 못 건졌으면 예외. 대상 자체가 0 이면 판단하지 않는다."""
    if candidates > 0 and collected == 0:
        raise CollectionSourceDeadError(
            f"{job_name}: 대상 {candidates}종목 중 한 건도 수집하지 못했다 — 출처 확인 필요"
        )


__all__ = ["CollectionSourceDeadError", "raise_if_nothing_collected"]
