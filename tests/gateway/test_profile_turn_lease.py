"""Equal BotMode session ids in different profile databases never share a turn lease."""
import asyncio

import pytest

from gateway.turn_lease import SessionTurnLeaseRegistry


@pytest.mark.asyncio
async def test_two_home_sessions_can_hold_leases_concurrently_and_rotate_independently():
    registry = SessionTurnLeaseRegistry()
    first = await registry.acquire('agentready_home', owner_key='alpha-route', generation=1,
                                   profile_home='/fixture/alpha')
    second = await registry.acquire('agentready_home', owner_key='bravo-route', generation=1,
                                    profile_home='/fixture/bravo', timeout=2)
    try:
        assert first.lease is not second.lease
        assert registry.rebind(first, 'compressed-tip')
        third = await registry.acquire('compressed-tip', owner_key='bravo-tip', generation=2,
                                      profile_home='/fixture/bravo', timeout=2)
        registry.release(third)
        assert first.profile_home == '/fixture/alpha'
    finally:
        registry.release(first)
        registry.release(second)


@pytest.mark.asyncio
async def test_alias_routes_in_one_profile_still_wait_for_the_owner():
    registry = SessionTurnLeaseRegistry()
    first = await registry.acquire('agentready_home', owner_key='alpha-route', generation=1,
                                   profile_home='/fixture/alpha')
    attempted = asyncio.Event()

    async def alias():
        attempted.set()
        return await registry.acquire('agentready_home', owner_key='same-profile-alias', generation=2,
                                      profile_home='/fixture/alpha', timeout=2)

    waiter = asyncio.create_task(alias())
    await attempted.wait()
    assert not waiter.done()
    registry.release(first)
    second = await waiter
    assert second.lease is first.lease
    registry.release(second)
