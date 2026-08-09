"""全局大模型调用池：并发上限。"""

import asyncio

from app.service.llm_call_pool import LlmCallPool


def test_llm_call_pool_limits_in_flight():
    pool = LlmCallPool(concurrency=2)
    peak = 0
    active = 0

    async def job():
        nonlocal peak, active
        async with pool.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    async def scenario():
        await asyncio.gather(*(job() for _ in range(6)))

    asyncio.run(scenario())
    assert peak == 2, f"并发峰值应为 2，实际 {peak}"
