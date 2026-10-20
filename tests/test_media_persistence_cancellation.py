import asyncio
from dataclasses import replace
import json
import threading

import pytest

from tests.test_chatbot import event
from tests.test_douyin import SHORT, options
from wxhook_api.core.douyin_config import MediaRetention
from wxhook_api.schemas.events import CallbackEvent
from wxhook_api.services.douyin import DouyinService


@pytest.mark.parametrize("fail_final", [False, True])
async def test_cancelled_media_worker_finishes_writes_before_releasing_job(tmp_path, monkeypatch, fail_final):
    service = DouyinService(replace(options(tmp_path), retention=MediaRetention(enabled=False)))
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    finished = [threading.Event(), threading.Event()]
    original = service._save
    states = []

    def delayed(job):
        index = len(states)
        states.append(job["status"])
        entered[index].set()
        try:
            assert release[index].wait(5)
            if index == 1 and fail_final:
                raise OSError("synthetic final save failure")
            original(job)
        finally:
            finished[index].set()

    monkeypatch.setattr(service, "_save", delayed)
    await service.start()
    worker = service._worker
    try:
        await service.receive(CallbackEvent.model_validate(event(SHORT)))
        assert await asyncio.to_thread(entered[0].wait, 3)
        job = service.snapshot()["jobs"][0]
        worker.cancel()
        await asyncio.sleep(0)
        worker.cancel()
        await asyncio.sleep(0)
        assert states == ["parsing"] and not worker.done()
        assert job["id"] in service._active_jobs
        release[0].set()
        assert await asyncio.to_thread(entered[1].wait, 3)
        assert finished[0].is_set() and states == ["parsing", "interrupted"]
        worker.cancel()
        await asyncio.sleep(0)
        worker.cancel()
        await asyncio.sleep(0)
        assert not worker.done() and job["id"] in service._active_jobs
        release[1].set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 3)
        await asyncio.wait_for(service._queue.join(), 2)
        assert finished[1].is_set() and job["id"] not in service._active_jobs
        if fail_final:
            assert job["storage_error"]
        else:
            saved = json.loads((service.options.output_dir / job["id"] / "result.json").read_text(encoding="utf-8"))
            assert saved["status"] == "interrupted" and saved["finished_at"]
            assert not job.get("storage_error")
    finally:
        for gate in release:
            gate.set()
        await asyncio.gather(worker, return_exceptions=True)
        await service.stop()
