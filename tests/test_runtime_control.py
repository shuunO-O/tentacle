import asyncio
from types import SimpleNamespace

from runtime.control import RestartController


async def test_restart_waits_for_current_round_and_coalesces_requests():
    session = SimpleNamespace(_lock=asyncio.Lock(), _restarting=False)
    controller = RestartController(session)
    await session._lock.acquire()
    await controller.request()
    first = controller._task
    await controller.request()
    await asyncio.sleep(0)
    assert controller._task is first and session._restarting
    assert controller.state == "waiting" and not controller.ready.is_set()
    session._lock.release()
    await first
    assert controller.ready.is_set() and controller.state == "restarting"
    await controller.close()
