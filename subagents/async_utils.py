"""取消安全的事务/资源回收等待。"""

import asyncio


async def settle(awaitable):
    """外层被重复取消时仍等待内层结束，然后把取消交回调用者。"""
    pending = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(pending)
            break
        except asyncio.CancelledError:
            if pending.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result
