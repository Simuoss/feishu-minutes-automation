"""全局大模型调用池：并发上限。"""

import asyncio

from app.service.llm_call_pool import LlmCallPool


def test_llm_call_pool_wait_hook():
    from app.service.llm_call_pool import reset_wait_stage_hook, set_wait_stage_hook

    pool = LlmCallPool(concurrency=1)
    stages: list[str] = []
    first_inside = asyncio.Event()
    release_first = asyncio.Event()

    async def first():
        async with pool.slot():
            first_inside.set()
            await release_first.wait()

    async def second():
        await first_inside.wait()
        token = set_wait_stage_hook(stages.append)
        try:
            occupy = asyncio.create_task(_occupy())
            for _ in range(30):
                if stages:
                    break
                await asyncio.sleep(0)
            release_first.set()
            await occupy
        finally:
            reset_wait_stage_hook(token)

    async def _occupy():
        async with pool.slot():
            pass

    async def scenario():
        await asyncio.gather(first(), second())

    asyncio.run(scenario())
    assert "在等模型槽位" in stages


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
