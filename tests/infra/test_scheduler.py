"""SchedulerRunner 단위 테스트 — FakeSchedulerStore 로 DB 없이 검증.

검증 대상:
- reload_once 가 신규/변경/삭제를 감지해 scheduler 에 반영
- run_job 이 handler 호출 + store.record_run_start/end 훅을 올바르게 부름
- 실패 시 status='failed' + error 문자열 전달
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from prime_jennie_runtime.infra.scheduler import (
    JobSpec,
    SchedulerRunner,
    SchedulerStore,
)


@dataclass
class FakeStore:
    specs: list[JobSpec] = field(default_factory=list)
    run_starts: list[tuple[str, datetime]] = field(default_factory=list)
    run_ends: list[dict] = field(default_factory=list)
    _next_run_id: int = 100

    async def fetch_specs(self, owner: str) -> list[JobSpec]:
        return list(self.specs)

    async def record_run_start(self, job_id: str, started_at: datetime) -> int:
        self._next_run_id += 1
        self.run_starts.append((job_id, started_at))
        return self._next_run_id

    async def record_run_end(
        self,
        job_id: str,
        run_id: int,
        *,
        status: str,
        error: str | None,
        duration_ms: int,
        next_run_at: datetime | None,
    ) -> None:
        self.run_ends.append(
            {
                "job_id": job_id,
                "run_id": run_id,
                "status": status,
                "error": error,
                "duration_ms": duration_ms,
                "next_run_at": next_run_at,
            }
        )


def _spec(
    job_id: str,
    handler_key: str = "handler_a",
    cron: str = "*/5 * * * *",
    enabled: bool = True,
    kwargs: dict | None = None,
) -> JobSpec:
    return JobSpec(
        id=job_id, handler_key=handler_key, cron=cron, kwargs=kwargs or {}, enabled=enabled
    )


def test_fake_store_satisfies_protocol():
    assert isinstance(FakeStore(), SchedulerStore)


def test_jobspec_signature_changes_on_mutation():
    a = _spec("j1", cron="*/5 * * * *")
    b = _spec("j1", cron="*/10 * * * *")
    assert a.signature != b.signature


async def test_reload_adds_new_job_to_scheduler():
    store = FakeStore(specs=[_spec("j1")])
    calls: list[dict] = []

    async def handler_a(**kwargs):
        calls.append(kwargs)

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
    )
    await runner.reload_once()
    assert runner._scheduler.get_job("j1") is not None
    assert "j1" in runner._known


async def test_reload_removes_disabled_job():
    store = FakeStore(specs=[_spec("j1", enabled=True)])

    async def handler_a(**kwargs):
        pass

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
    )
    await runner.reload_once()
    assert runner._scheduler.get_job("j1") is not None

    store.specs = [_spec("j1", enabled=False)]
    await runner.reload_once()
    assert runner._scheduler.get_job("j1") is None


async def test_reload_removes_deleted_job():
    store = FakeStore(specs=[_spec("j1"), _spec("j2")])

    async def handler_a(**kwargs):
        pass

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
    )
    await runner.reload_once()
    assert runner._scheduler.get_job("j1") is not None
    assert runner._scheduler.get_job("j2") is not None

    store.specs = [_spec("j1")]
    await runner.reload_once()
    assert runner._scheduler.get_job("j2") is None
    assert "j2" not in runner._known


async def test_reload_reschedules_on_cron_change():
    store = FakeStore(specs=[_spec("j1", cron="*/5 * * * *")])

    async def handler_a(**kwargs):
        pass

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
    )
    await runner.reload_once()
    old_sig = runner._known["j1"]

    store.specs = [_spec("j1", cron="*/10 * * * *")]
    await runner.reload_once()
    new_sig = runner._known["j1"]
    assert old_sig != new_sig


async def test_reload_skips_unchanged_job():
    store = FakeStore(specs=[_spec("j1")])
    add_count = 0

    async def handler_a(**kwargs):
        pass

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
    )
    original_add = runner._add_job

    def counting_add(spec):
        nonlocal add_count
        add_count += 1
        original_add(spec)

    runner._add_job = counting_add  # type: ignore[method-assign]

    await runner.reload_once()
    first = add_count
    await runner.reload_once()
    assert add_count == first  # signature 동일 → 재등록 없음


async def test_reload_ignores_unknown_handler_key():
    store = FakeStore(specs=[_spec("j1", handler_key="does_not_exist")])

    runner = SchedulerRunner(
        owner="test",
        handlers={},
        store=store,
    )
    # 에러 없이 pass, scheduler 에는 등록 안됨
    await runner.reload_once()
    assert runner._scheduler.get_job("j1") is None


async def test_reload_ignores_invalid_cron():
    store = FakeStore(specs=[_spec("j1", cron="not a cron expr")])

    async def handler_a(**kwargs):
        pass

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
    )
    await runner.reload_once()
    assert runner._scheduler.get_job("j1") is None


async def test_run_job_success_records_start_and_end():
    store = FakeStore()
    calls: list[dict] = []

    async def handler_a(**kwargs):
        calls.append(kwargs)

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
    )
    await runner.run_job(
        job_id="j1",
        handler=handler_a,
        handler_kwargs={"universe": ["005930"]},
    )
    assert calls == [{"universe": ["005930"]}]
    assert len(store.run_starts) == 1
    assert len(store.run_ends) == 1
    end = store.run_ends[0]
    assert end["status"] == "success"
    assert end["error"] is None
    assert end["duration_ms"] >= 0


async def test_run_job_failure_records_failed_status():
    store = FakeStore()

    async def handler_a(**kwargs):
        raise RuntimeError("boom")

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
        max_retries=0,
    )
    await runner.run_job(
        job_id="j1",
        handler=handler_a,
        handler_kwargs={},
    )
    assert len(store.run_ends) == 1
    end = store.run_ends[0]
    assert end["status"] == "failed"
    assert "RuntimeError" in (end["error"] or "")
    assert "boom" in (end["error"] or "")


async def test_run_job_truncates_long_error_message():
    store = FakeStore()

    async def handler_a(**kwargs):
        raise ValueError("x" * 1000)

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
        max_retries=0,
    )
    await runner.run_job(job_id="j1", handler=handler_a, handler_kwargs={})
    assert len(store.run_ends[0]["error"]) <= 500


# -----------------------------------------------------------------------------
# run_job retry — handler 실패 시 max_retries 회까지 재시도 (v2 Airflow retries 대체)
# -----------------------------------------------------------------------------


async def test_run_job_retries_then_succeeds():
    """첫 시도 실패 → 재시도에서 성공 → status='success' + handler 2회 호출."""
    store = FakeStore()
    attempts = {"n": 0}

    async def flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ConnectionError("transient")

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": flaky},
        store=store,
        max_retries=1,
        retry_delay_sec=0.0,
    )
    await runner.run_job(job_id="j1", handler=flaky, handler_kwargs={})
    assert attempts["n"] == 2  # 최초 1회 + 재시도 1회
    assert len(store.run_ends) == 1
    end = store.run_ends[0]
    assert end["status"] == "success"
    assert end["error"] is None


async def test_run_job_retry_exhausted_records_failed():
    """모든 시도 실패 → status='failed', handler 가 max_retries+1 회 호출됨."""
    store = FakeStore()
    attempts = {"n": 0}

    async def always_fail(**kwargs):
        attempts["n"] += 1
        raise RuntimeError("boom")

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": always_fail},
        store=store,
        max_retries=2,
        retry_delay_sec=0.0,
    )
    await runner.run_job(job_id="j1", handler=always_fail, handler_kwargs={})
    assert attempts["n"] == 3  # 최초 1회 + 재시도 2회
    end = store.run_ends[0]
    assert end["status"] == "failed"
    assert "RuntimeError" in (end["error"] or "")
    assert "after 3 attempts" in (end["error"] or "")


async def test_run_job_no_retry_when_max_retries_zero():
    """max_retries=0 → handler 1회만 호출, 재시도 없음."""
    store = FakeStore()
    attempts = {"n": 0}

    async def always_fail(**kwargs):
        attempts["n"] += 1
        raise RuntimeError("boom")

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": always_fail},
        store=store,
        max_retries=0,
    )
    await runner.run_job(job_id="j1", handler=always_fail, handler_kwargs={})
    assert attempts["n"] == 1
    assert store.run_ends[0]["status"] == "failed"


async def test_start_and_stop_lifecycle():
    store = FakeStore(specs=[_spec("j1", cron="*/1 * * * *")])

    async def handler_a(**kwargs):
        pass

    runner = SchedulerRunner(
        owner="test",
        handlers={"handler_a": handler_a},
        store=store,
        poll_interval_sec=0.1,
    )
    await runner.start()
    # poll 루프가 즉시 한 번 더 reload 하지만 동일 spec 이라 no-op
    await asyncio.sleep(0.15)
    await runner.stop()
    assert runner._poll_task is not None
    assert runner._poll_task.cancelled() or runner._poll_task.done()


# -----------------------------------------------------------------------------
# cron dow 치환 — cron 표준(0/7=Sun,1=Mon) → apscheduler(0=Mon,6=Sun)
# -----------------------------------------------------------------------------


def test_cron_dow_translation_range_mon_fri():
    from prime_jennie_runtime.infra.scheduler import _normalize_cron_for_apscheduler

    # cron "1-5" (Mon-Fri) → apscheduler "0-4"
    assert _normalize_cron_for_apscheduler("*/5 9-15 * * 1-5") == "*/5 9-15 * * 0-4"


def test_cron_dow_translation_sunday_both_forms():
    from prime_jennie_runtime.infra.scheduler import _normalize_cron_for_apscheduler

    # cron "0" = Sun → apscheduler "6"
    assert _normalize_cron_for_apscheduler("0 20 * * 0") == "0 20 * * 6"
    # cron "7" 도 Sun → apscheduler "6"
    assert _normalize_cron_for_apscheduler("0 20 * * 7") == "0 20 * * 6"


def test_cron_dow_translation_list_and_named():
    from prime_jennie_runtime.infra.scheduler import _normalize_cron_for_apscheduler

    # 리스트: cron "1,4" (Mon,Thu) → apscheduler "0,3"
    assert _normalize_cron_for_apscheduler("0 6 * * 1,4") == "0 6 * * 0,3"
    # 이름: cron "mon-fri" → apscheduler "0-4"
    assert _normalize_cron_for_apscheduler("0 9 * * mon-fri") == "0 9 * * 0-4"


def test_cron_dow_translation_tue_sat_for_us_market():
    from prime_jennie_runtime.infra.scheduler import _normalize_cron_for_apscheduler

    # cron "2-6" (Tue-Sat) → apscheduler "1-5"
    assert _normalize_cron_for_apscheduler("0 7 * * 2-6") == "0 7 * * 1-5"


def test_cron_dow_translation_wildcard_unchanged():
    from prime_jennie_runtime.infra.scheduler import _normalize_cron_for_apscheduler

    assert _normalize_cron_for_apscheduler("0 3 * * *") == "0 3 * * *"


def test_cron_non_5_fields_untouched():
    from prime_jennie_runtime.infra.scheduler import _normalize_cron_for_apscheduler

    # 4 필드짜리 (형식 오류) 는 그대로 전달 → apscheduler 가 ValueError 처리
    assert _normalize_cron_for_apscheduler("0 3 * *") == "0 3 * *"
