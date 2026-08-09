"""纪要作业协调：单飞，不再做作业级并发上限。"""

import asyncio

from app.service.summary_event_broker import SummaryStatus, summary_broker
from app.service.summary_generation_pool import SummaryGenerationPool

OWNER = 1


def test_pool_allows_parallel_jobs_for_different_tokens():
    """作业侧不限并发；峰值可超过旧版 semaphore 上限。"""
    pool = SummaryGenerationPool()
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
        results = await asyncio.gather(
            *(pool.submit(t, job, owner_user_id=OWNER) for t in tokens)
        )
        for t in tokens:
            summary_broker.clear(t, owner_user_id=OWNER)
        return results

    results = asyncio.run(scenario())
    assert results == ["ok"] * 5
    assert peak == 5, f"不同 token 应可并行，峰值应为 5，实际 {peak}"


def test_pool_sync_broker_status_reflects_running_task():
    pool = SummaryGenerationPool()

    async def job():
        pool.sync_broker_status("a", owner_user_id=OWNER)
        channel = summary_broker.get("a", owner_user_id=OWNER)
        assert channel is not None
        assert channel.status == SummaryStatus.GENERATING.value
        await asyncio.sleep(0.01)

    async def scenario():
        # 预先放一个 channel，sync 才能把 stage 写成生成中
        summary_broker.update(
            "a", owner_user_id=OWNER, percent=5, stage="排队中"
        )
        await pool.submit("a", job, owner_user_id=OWNER)
        summary_broker.clear("a", owner_user_id=OWNER)

    asyncio.run(scenario())


def test_pool_tracks_active_tokens():
    pool = SummaryGenerationPool()
    observed: list[bool] = []

    async def job():
        observed.append(pool.is_active("x", owner_user_id=OWNER))
        await asyncio.sleep(0.01)

    async def scenario():
        assert pool.is_active("x", owner_user_id=OWNER) is False
        await pool.submit("x", job, owner_user_id=OWNER)
        summary_broker.clear("x", owner_user_id=OWNER)
        assert pool.is_active("x", owner_user_id=OWNER) is False

    asyncio.run(scenario())
    assert observed == [True]


def test_pool_single_flight_awaits_same_future():
    pool = SummaryGenerationPool()
    runs = 0

    async def job():
        nonlocal runs
        runs += 1
        await asyncio.sleep(0.05)
        return "ok"

    async def scenario():
        first = asyncio.create_task(pool.submit("x", job, owner_user_id=OWNER))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(pool.submit("x", job, owner_user_id=OWNER))
        results = await asyncio.gather(first, second)
        summary_broker.clear("x", owner_user_id=OWNER)
        return results

    results = asyncio.run(scenario())
    assert results == ["ok", "ok"]
    assert runs == 1, f"同 (owner, token) 应只跑一次，实际 {runs}"
