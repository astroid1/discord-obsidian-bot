"""Single-worker job queue: the GPU and the vault are used by one job at a time."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .models import IngestItem, RunResult
from .pipeline import Pipeline, Progress

log = logging.getLogger(__name__)

RETRY_DELAYS = (15, 60)


class TransientError(RuntimeError):
    """Raise from a provider to request a retry."""


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TransientError | httpx.TransportError | asyncio.TimeoutError):
        return True
    try:
        import anthropic

        if isinstance(
            exc,
            anthropic.APIConnectionError | anthropic.RateLimitError | anthropic.InternalServerError,
        ):
            return True
    except ImportError:
        pass
    return False


class JobSink(Progress, Protocol):
    async def queued(self, position: int) -> None: ...

    async def done(self, result: RunResult) -> None: ...

    async def failed(self, error: str, will_retry: bool) -> None: ...


class LogSink:
    def __init__(self, name: str = "") -> None:
        self.name = name

    async def queued(self, position: int) -> None:
        log.info("[%s] queued #%d", self.name, position)

    async def update(self, stage: str, detail: str = "", fraction: float | None = None) -> None:
        pct = f" {fraction * 100:.0f}%" if fraction is not None else ""
        log.info("[%s] %s%s %s", self.name, stage, pct, detail)

    async def done(self, result: RunResult) -> None:
        log.info(
            "[%s] %s: %s",
            self.name,
            result.status,
            result.note.note_path if result.note else result.message,
        )

    async def failed(self, error: str, will_retry: bool) -> None:
        log.error(
            "[%s] failed (%s): %s", self.name, "will retry" if will_retry else "gave up", error
        )


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    item: IngestItem = field(compare=False)
    sink: JobSink = field(compare=False)
    attempt: int = field(default=0, compare=False)


class JobQueue:
    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        self._q: asyncio.PriorityQueue[_Job] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._worker: asyncio.Task | None = None
        self.current: IngestItem | None = None
        # Called after a job finishes with status 'done' (suggested tasks, meeting cleanup).
        self.on_result = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._loop(), name="dob-worker")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def size(self) -> int:
        return self._q.qsize() + (1 if self.current else 0)

    async def submit(
        self, item: IngestItem, sink: JobSink | None = None, *, persist: bool = True
    ) -> int:
        sink = sink or LogSink(item.original_name)
        if persist:
            self.pipeline.state.submit(item)
        job = _Job(item.priority, next(self._seq), item, sink)
        await self._q.put(job)
        pos = self.size()
        await sink.queued(pos)
        return pos

    async def requeue_inflight(self) -> int:
        items = self.pipeline.state.inflight()
        for it in items:
            await self.submit(it, persist=False)
        if items:
            log.info("re-queued %d in-flight job(s) from a previous run", len(items))
        return len(items)

    async def _loop(self) -> None:
        while True:
            job = await self._q.get()
            self.current = job.item
            try:
                await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("unexpected worker error")
            finally:
                self.current = None
                self._q.task_done()

    async def _run_job(self, job: _Job) -> None:
        try:
            result = await self.pipeline.run(job.item, job.sink)
        except Exception as e:  # noqa: BLE001
            transient = is_transient(e) and job.attempt < len(RETRY_DELAYS)
            msg = f"{type(e).__name__}: {e}"
            log.warning("job %s failed: %s", job.item.original_name, msg, exc_info=not transient)
            await job.sink.failed(msg, will_retry=transient)
            if transient:
                delay = RETRY_DELAYS[job.attempt]
                job.attempt += 1
                asyncio.get_running_loop().call_later(delay, self._q.put_nowait, job)
            return
        await job.sink.done(result)
        if self.on_result is not None and result.status == "done":
            try:
                await self.on_result(job.item, result)
            except Exception:  # noqa: BLE001 - a hook must never break the worker
                log.exception("on_result hook failed for %s", job.item.original_name)
