"""下载池的并发上限与排队行为。"""

import asyncio

from app.service.download_pool import DownloadPool
from app.service.download_progress_store import download_progress_store


def test_pool_limits_concurrency_to_configured_value():
    pool = DownloadPool(concurrency=5)
    peak = 0
    active = 0

    async def job():
        nonlocal peak, active
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return "ok"

    async def scenario():
        tokens = [f"tok{i}" for i in range(12)]
        results = await asyncio.gather(*(pool.submit(t, job) for t in tokens))
        for t in tokens:
            download_progress_store.clear(t)
        return results

    results = asyncio.run(scenario())

    assert results == ["ok"] * 12
    assert peak == 5, f"并发峰值应被限制为 5，实际 {peak}"


def test_pool_reports_queue_stage_for_waiting_tasks():
    pool = DownloadPool(concurrency=1)

    async def job():
        await asyncio.sleep(0.05)

    async def scenario():
        first = asyncio.create_task(pool.submit("a", job))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(pool.submit("b", job))
        await asyncio.sleep(0.01)

        progress_b = download_progress_store.get("b")
        assert progress_b is not None
        assert "排队中" in progress_b.stage

        await asyncio.gather(first, second)
        download_progress_store.clear("a")
        download_progress_store.clear("b")

    asyncio.run(scenario())


def test_pool_tracks_active_tokens():
    pool = DownloadPool(concurrency=1)
    observed: list[bool] = []

    async def job():
        observed.append(pool.is_active("x"))
        await asyncio.sleep(0.01)

    async def scenario():
        assert pool.is_active("x") is False
        await pool.submit("x", job)
        download_progress_store.clear("x")
        assert pool.is_active("x") is False

    asyncio.run(scenario())
    assert observed == [True]
