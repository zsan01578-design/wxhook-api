import asyncio


async def finish_before_cancel(operation):
    """Keep a commit's lock until its disk write and memory update have settled.

    Cancelling an await on to_thread cannot stop its filesystem write. Shield
    the whole commit, drain it even after repeated cancellation, then propagate
    cancellation to the caller. A failed commit still leaves its old state.
    """
    task = asyncio.create_task(operation)
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = exc
        except Exception:
            if cancellation is not None:
                raise cancellation from None
            raise
    if cancellation is not None:
        raise cancellation
    return result
