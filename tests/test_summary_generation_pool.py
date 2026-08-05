"""生成池的并发上限与排队行为。"""

import asyncio

from app.service.summary_event_broker import SummaryStatus, summary_broker
from app.service.summary_generation_pool import SummaryGenerationPool


def test_pool_limits_concurrency_to_configured_value():
    pool = SummaryGenerationPool(concurrency=2)
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
        tokens = [f"tok{i}" for i in range(5)]
        results = await asyncio.gather(*(pool.submit(t, job) for t in tokens))
        for t in tokens:
            summary_broker.clear(t)
        return results

    results = asyncio.run(scenario())

    assert results == ["ok"] * 5
    assert peak == 2, f"并发峰值应被限制为 2，实际 {peak}"


def test_pool_reports_queue_position_for_waiting_tasks():
    pool = SummaryGenerationPool(concurrency=1)

    async def job():
        await asyncio.sleep(0.05)

    async def scenario():
        first = asyncio.create_task(pool.submit("a", job))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(pool.submit("b", job))
        await asyncio.sleep(0.01)

        # 第一个已在执行，第二个应处于排队状态
        channel_b = summary_broker.get("b")
        assert channel_b is not None
        assert channel_b.status == SummaryStatus.QUEUED.value

        await asyncio.gather(first, second)
        summary_broker.clear("a")
        summary_broker.clear("b")

    asyncio.run(scenario())


def test_pool_sync_broker_status_reflects_running_task():
    pool = SummaryGenerationPool(concurrency=1)

    async def job():
        pool.sync_broker_status("a")
        channel = summary_broker.get("a")
        assert channel is not None
        assert channel.status == SummaryStatus.GENERATING.value
        await asyncio.sleep(0.01)

    async def scenario():
        await pool.submit("a", job)
        summary_broker.clear("a")

    asyncio.run(scenario())


def test_pool_tracks_active_tokens():
    pool = SummaryGenerationPool(concurrency=1)
    observed: list[bool] = []

    async def job():
        observed.append(pool.is_active("x"))
        await asyncio.sleep(0.01)

    async def scenario():
        assert pool.is_active("x") is False
        await pool.submit("x", job)
        summary_broker.clear("x")
        assert pool.is_active("x") is False

    asyncio.run(scenario())
    assert observed == [True]
