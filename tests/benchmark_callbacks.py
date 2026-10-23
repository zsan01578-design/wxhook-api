"""Isolated I/O-wait comparison: python -m tests.benchmark_callbacks.

This is not a hardware capacity test: no real Hook, model or database is used.
"""

import asyncio
import json
import time

from wxhook_api.services.events import EventDispatcher


async def measure(workers, messages=120, delay=0.02):
    dispatcher = EventDispatcher(maxsize=messages, workers=workers)
    completed = 0
    active = peak = 0

    async def handle(event):
        nonlocal completed, active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(delay)
            completed += 1
        finally:
            active -= 1

    dispatcher.register(handle)
    await dispatcher.start()
    start = time.perf_counter()
    try:
        for index in range(messages):
            group = f"g{index % 8}@chatroom"
            accepted = await dispatcher.enqueue(dict(event_type=1001, msgid=index, type=1,
                timestamp=1, wxid=group, sender="member", roomid=group, content="mock callback"))
            assert accepted
        await asyncio.wait_for(dispatcher._queue.join(), 15)
        elapsed = time.perf_counter() - start
    finally:
        await dispatcher.stop()
    assert completed == messages
    return dict(workers=workers, messages=completed, peak_in_flight=peak, seconds=round(elapsed, 3))


async def main():
    serial = await measure(1)
    concurrent = await measure(4)
    print(json.dumps(dict(scenario="120 callbacks, 8 conversations, simulated 20ms I/O each",
        serial=serial, concurrent=concurrent,
        speedup=round(serial["seconds"] / concurrent["seconds"], 2)), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
