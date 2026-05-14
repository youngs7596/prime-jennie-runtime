"""Coordinator events Pydantic schema roundtrip 테스트.

design: .ai/designs/2026-05-14-agent-coordinator.md §3 Layer C (Event Bus)

검증 범위:
  1. 9 종 이벤트 각각 instance 생성 → model_dump_json() → model_validate_json() 동일성.
  2. Discriminated Union ``CoordinatorEvent`` 가 event_kind 필드로 올바른 subclass
     로 dispatch (TypeAdapter 경유).
  3. 필수 필드 누락 시 ValidationError 발생 (대표 1~2 케이스).

DB / Redis 의존 없음. ``pytest tests/coordinator/test_event_roundtrip.py`` 단독 실행 가능.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from prime_jennie_runtime.coordinator.events import (
    CoordinatorEvent,
    EntryDecidedEvent,
    EntryFilledEvent,
    EntryQueuedEvent,
    EntryRejectedEvent,
    ExitDecidedEvent,
    ExitFilledEvent,
    ExternalEvent,
    PolicyChangedEvent,
    SheetPublishedEvent,
)

# Discriminated Union 은 type alias 라 직접 model_validate_json 호출 불가.
# TypeAdapter 로 감싸야 한다.
EVENT_ADAPTER: TypeAdapter[CoordinatorEvent] = TypeAdapter(CoordinatorEvent)


def _now() -> datetime:
    """timezone-aware UTC. Pydantic 직렬화/역직렬화 시 tz 보존 확인용."""
    return datetime(2026, 5, 14, 9, 30, 0, tzinfo=UTC)


# ─────────────────────────────────────────────────────────────────────
# 개별 모델 round-trip
# ─────────────────────────────────────────────────────────────────────


def test_sheet_published_roundtrip():
    ev = SheetPublishedEvent(
        occurred_at=_now(),
        correlation_id="sheet-001",
        sheet_id="sheet-001",
        ticker="005930",
        strategy_tag="momentum",
        conviction=0.78,
        final_pct=2.5,
        macro_run_id="macro-2026-05-14",
        scout_run_id="scout-2026-05-14",
        macro_gate="open",
        macro_size_mult=1.0,
        risk_level="normal",
        sheet_generated_at=_now(),
    )
    restored = SheetPublishedEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev
    assert restored.event_kind == "sheet_published"


def test_entry_queued_roundtrip():
    ev = EntryQueuedEvent(
        occurred_at=_now(),
        correlation_id="sheet-002",
        sheet_id="sheet-002",
        ticker="000660",
        queue_size_after=3,
    )
    restored = EntryQueuedEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev


def test_entry_decided_roundtrip():
    ev = EntryDecidedEvent(
        occurred_at=_now(),
        correlation_id="sheet-003",
        sheet_id="sheet-003",
        ticker="035720",
        conditions_passed=True,
        passed_reason="all_passed",
        intended_quantity=10,
        decision_id=42,
    )
    restored = EntryDecidedEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev


def test_entry_filled_roundtrip():
    ev = EntryFilledEvent(
        occurred_at=_now(),
        correlation_id="sheet-004",
        sheet_id="sheet-004",
        ticker="005380",
        filled_qty=8,
        filled_price=124500.0,
        intended_quantity=10,
        is_partial=True,
        order_no="0000123456",
    )
    restored = EntryFilledEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev
    assert restored.is_partial is True


def test_entry_rejected_roundtrip():
    ev = EntryRejectedEvent(
        occurred_at=_now(),
        correlation_id="sheet-005",
        sheet_id="sheet-005",
        ticker="066570",
        reason="already_holding",
        details="position exists for ticker",
    )
    restored = EntryRejectedEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev


def test_exit_decided_roundtrip():
    ev = ExitDecidedEvent(
        occurred_at=_now(),
        correlation_id="sheet-006",
        sheet_id="sheet-006",
        ticker="005930",
        rule_type="trailing_tp",
        reason="trailing stop hit",
        portion=0.5,
        metadata={"trail_pct": 3.0, "peak_price": 78000},
        decision_id=99,
    )
    restored = ExitDecidedEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev
    assert restored.metadata["peak_price"] == 78000


def test_exit_filled_roundtrip():
    ev = ExitFilledEvent(
        occurred_at=_now(),
        correlation_id="sheet-007",
        sheet_id="sheet-007",
        ticker="000660",
        filled_qty=10,
        filled_price=160000.0,
        entry_price=150000.0,
        reason="trailing_tp",
        fully_closed=True,
        pnl_amount=100000.0,
        pnl_pct=6.67,
    )
    restored = ExitFilledEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev


def test_policy_changed_roundtrip():
    ev = PolicyChangedEvent(
        occurred_at=_now(),
        correlation_id="policy-macro-2026-05-14",
        policy_name="macro_gate",
        prev_value="open",
        new_value="closed",
        metadata={"reason": "vix_spike"},
    )
    restored = PolicyChangedEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev


def test_external_event_roundtrip():
    ev = ExternalEvent(
        occurred_at=_now(),
        correlation_id="ext-001",
        event_subtype="kis_state_mismatch",
        ticker="005930",
        metadata={"local_qty": 10, "kis_qty": 8},
    )
    restored = ExternalEvent.model_validate_json(ev.model_dump_json())
    assert restored == ev


# ─────────────────────────────────────────────────────────────────────
# Discriminated Union dispatch
# ─────────────────────────────────────────────────────────────────────


def test_discriminated_union_dispatches_to_sheet_published():
    raw = SheetPublishedEvent(
        occurred_at=_now(),
        correlation_id="sheet-100",
        sheet_id="sheet-100",
        ticker="005930",
        strategy_tag="momentum",
        final_pct=2.0,
        sheet_generated_at=_now(),
    ).model_dump_json()

    parsed = EVENT_ADAPTER.validate_json(raw)
    assert isinstance(parsed, SheetPublishedEvent)
    assert parsed.event_kind == "sheet_published"
    assert parsed.sheet_id == "sheet-100"


def test_discriminated_union_dispatches_all_kinds():
    """9 종 이벤트 모두 union 으로 parse 시 정확한 subclass 로 들어오는지."""
    samples: list[tuple[type, dict]] = [
        (
            SheetPublishedEvent,
            {
                "occurred_at": _now(),
                "correlation_id": "c1",
                "sheet_id": "s1",
                "ticker": "005930",
                "strategy_tag": "momentum",
                "final_pct": 1.0,
                "sheet_generated_at": _now(),
            },
        ),
        (
            EntryQueuedEvent,
            {
                "occurred_at": _now(),
                "correlation_id": "c2",
                "sheet_id": "s2",
                "ticker": "000660",
                "queue_size_after": 1,
            },
        ),
        (
            EntryDecidedEvent,
            {
                "occurred_at": _now(),
                "correlation_id": "c3",
                "sheet_id": "s3",
                "ticker": "035720",
                "conditions_passed": False,
                "passed_reason": "no_conditions",
                "intended_quantity": 0,
            },
        ),
        (
            EntryFilledEvent,
            {
                "occurred_at": _now(),
                "correlation_id": "c4",
                "sheet_id": "s4",
                "ticker": "005380",
                "filled_qty": 5,
                "filled_price": 100.0,
                "intended_quantity": 5,
                "is_partial": False,
            },
        ),
        (
            EntryRejectedEvent,
            {
                "occurred_at": _now(),
                "correlation_id": "c5",
                "sheet_id": "s5",
                "ticker": "066570",
                "reason": "sizer_zero",
            },
        ),
        (
            ExitDecidedEvent,
            {
                "occurred_at": _now(),
                "correlation_id": "c6",
                "sheet_id": "s6",
                "ticker": "005930",
                "rule_type": "fixed_sl",
                "reason": "sl_hit",
            },
        ),
        (
            ExitFilledEvent,
            {
                "occurred_at": _now(),
                "correlation_id": "c7",
                "sheet_id": "s7",
                "ticker": "000660",
                "filled_qty": 5,
                "filled_price": 95.0,
                "entry_price": 100.0,
                "reason": "fixed_sl",
                "fully_closed": True,
            },
        ),
        (
            PolicyChangedEvent,
            {
                "occurred_at": _now(),
                "correlation_id": "c8",
                "policy_name": "risk_level",
                "new_value": "elevated",
            },
        ),
        (
            ExternalEvent,
            {
                "occurred_at": _now(),
                "correlation_id": "c9",
                "event_subtype": "manual_trade_detected",
            },
        ),
    ]

    for cls, fields in samples:
        instance = cls(**fields)
        parsed = EVENT_ADAPTER.validate_json(instance.model_dump_json())
        assert isinstance(parsed, cls), f"{cls.__name__} 가 union dispatch 에서 빠짐"
        assert parsed.event_kind == instance.event_kind


# ─────────────────────────────────────────────────────────────────────
# 필수 필드 누락 시 ValidationError
# ─────────────────────────────────────────────────────────────────────


def test_missing_required_field_sheet_published_raises():
    """SheetPublishedEvent 에서 ticker (필수) 빼면 ValidationError."""
    with pytest.raises(ValidationError):
        SheetPublishedEvent(
            occurred_at=_now(),
            correlation_id="sheet-x",
            sheet_id="sheet-x",
            # ticker 누락
            strategy_tag="momentum",
            final_pct=1.0,
            sheet_generated_at=_now(),
        )


def test_missing_required_field_exit_filled_raises():
    """ExitFilledEvent 에서 entry_price (필수) 빼면 ValidationError."""
    with pytest.raises(ValidationError):
        ExitFilledEvent(
            occurred_at=_now(),
            correlation_id="sheet-y",
            sheet_id="sheet-y",
            ticker="005930",
            filled_qty=5,
            filled_price=100.0,
            # entry_price 누락
            reason="fixed_sl",
            fully_closed=True,
        )


def test_discriminated_union_unknown_kind_raises():
    """event_kind 값이 정의 외면 ValidationError."""
    bad_payload = (
        '{"event_kind": "nonexistent_kind", '
        '"occurred_at": "2026-05-14T09:30:00+00:00", '
        '"correlation_id": "x"}'
    )
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_json(bad_payload)
