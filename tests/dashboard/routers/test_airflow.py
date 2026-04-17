"""Airflow 라우터 happy path — scheduled_jobs CRUD + /dags 호환 응답."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient


async def test_list_jobs_empty(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/airflow/jobs")
        assert resp.status_code == 200
        assert resp.json() == []


async def test_create_list_get_patch_delete_job(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create = await client.post(
            "/api/airflow/jobs",
            json={
                "id": "news_pipeline.crawl_cycle",
                "owner": "news_pipeline",
                "handler_key": "crawl_cycle",
                "cron": "*/10 9-15 * * 1-5",
                "kwargs": {"batch_size": 20},
                "enabled": True,
            },
        )
        assert create.status_code == 201, create.text
        body = create.json()
        assert body["id"] == "news_pipeline.crawl_cycle"
        assert body["kwargs"] == {"batch_size": 20}

        got = await client.get("/api/airflow/jobs/news_pipeline.crawl_cycle")
        assert got.status_code == 200
        assert got.json()["cron"] == "*/10 9-15 * * 1-5"

        patch = await client.patch(
            "/api/airflow/jobs/news_pipeline.crawl_cycle",
            json={"enabled": False},
        )
        assert patch.status_code == 200
        assert patch.json()["enabled"] is False

        listed = await client.get("/api/airflow/jobs?owner=news_pipeline")
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        dags = await client.get("/api/airflow/dags")
        assert dags.status_code == 200
        # disabled 라 /dags 에는 노출되지 않음
        assert dags.json() == []

        deleted = await client.delete("/api/airflow/jobs/news_pipeline.crawl_cycle")
        assert deleted.status_code == 204

        not_found = await client.get("/api/airflow/jobs/news_pipeline.crawl_cycle")
        assert not_found.status_code == 404


async def test_dags_shape(app, session_factory):
    """/api/airflow/dags 는 v2 Airflow 응답 모양과 호환."""
    async with session_factory() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO scheduled_jobs (id, owner, handler_key, cron, enabled) "
                "VALUES ('slow_loop.scout_daily', 'slow_loop', 'scout_daily', '30 8-14 * * 1-5', 1)"
            )
        )
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/airflow/dags")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert row["dag_id"] == "slow_loop.scout_daily"
        assert row["schedule_interval"] == "30 8-14 * * 1-5"
        assert "last_run_state" in row
