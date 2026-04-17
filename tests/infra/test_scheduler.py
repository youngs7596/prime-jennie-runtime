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
    )
    await runner.run_job(job_id="j1", handler=handler_a, handler_kwargs={})
    assert len(store.run_ends[0]["error"]) <= 500


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
