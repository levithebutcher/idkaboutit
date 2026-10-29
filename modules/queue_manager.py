"""
modules/queue_manager.py
------------------------
Async job queue with concurrency limits, per-user quotas, and a fixed
number of worker tasks that pull jobs and call the orchestrator.

Responsibilities:
- Accept jobs from the bot's message handler (enqueue()).
- Enforce per-user active-job limit (per_user_job_limit).
- Enforce global queue depth limit (max_queue_depth).
- Run N workers (max_concurrent_jobs) that call the resolver.
- Deliver results back to the user via a callback.
- Clean up per-user state when jobs finish.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from config import settings
from modules.url_parser import ParsedURL
from resolvers.base import ResolutionResult
from resolvers.orchestrator import resolve_url
from utils.logger import get_logger, log_resolution

logger = get_logger(__name__)

# Callback signature: (user_id, chat_id, parsed_url, result) -> awaitable
ResultCallback = Callable[
    [int, int, ParsedURL, ResolutionResult],
    Awaitable[None],
]


# ---------- Job dataclass ----------

@dataclass
class Job:
    job_id: str
    user_id: int
    chat_id: int
    parsed_url: ParsedURL
    enqueued_at: float = field(default_factory=time.monotonic)


# ---------- Queue Manager ----------

class QueueManager:
    """
    Manages an asyncio queue, workers, and per-user active-job counts.
    """

    def __init__(self, on_result: ResultCallback) -> None:
        self._on_result = on_result
        self._queue: asyncio.Queue[Job | None] = asyncio.Queue(
            maxsize=settings.max_queue_depth
        )
        self._user_active: dict[int, int] = {}
        self._workers: list[asyncio.Task] = []
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._job_seq = 0
        self._running = False

    # ---------- Lifecycle ----------

    async def start(self) -> None:
        """Spawn worker tasks. Call once at bot startup."""
        if self._running:
            return
        self._running = True
        for i in range(settings.max_concurrent_jobs):
            task = asyncio.create_task(self._worker(i), name=f"worker-{i}")
            self._workers.append(task)
        logger.info("QueueManager started with %d workers",
                    settings.max_concurrent_jobs)

    async def stop(self) -> None:
        """Signal workers to exit, then wait for them to finish."""
        if not self._running:
            return
        self._running = False
        for _ in self._workers:
            await self._queue.put(None)  # sentinel
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("QueueManager stopped")

    # ---------- Enqueue ----------

    async def enqueue(
        self,
        user_id: int,
        chat_id: int,
        parsed_url: ParsedURL,
    ) -> tuple[bool, str]:
        """
        Try to enqueue a job. Returns (accepted, message).
        Caller sends `message` to the user if not accepted.
        """
        # Per-user limit
        active = self._user_active.get(user_id, 0)
        if active >= settings.per_user_job_limit:
            return False, (
                f"⚠️ Aapke paas pehle se {active} link(s) process ho rahe hain. "
                f"Thoda intezaar karein."
            )

        # Global queue depth
        if self._queue.full():
            return False, (
                "⚠️ Queue full hai. Thodi der baad try karein."
            )

        self._job_seq += 1
        job = Job(
            job_id=f"job-{self._job_seq}",
            user_id=user_id,
            chat_id=chat_id,
            parsed_url=parsed_url,
        )
        await self._queue.put(job)
        self._user_active[user_id] = active + 1

        position = self._queue.qsize()
        logger.info(
            "Enqueued %s for user %d (%s) — queue size %d",
            job.job_id, user_id, parsed_url.domain, position,
        )
        return True, f"✅ Queue mein add ho gaya (position: {position})"

    # ---------- Worker ----------

    async def _worker(self, worker_id: int) -> None:
        logger.debug("Worker %d started", worker_id)
        while True:
            job = await self._queue.get()
            if job is None:  # shutdown sentinel
                self._queue.task_done()
                logger.debug("Worker %d exiting", worker_id)
                return

            try:
                async with self._semaphore:
                    logger.info(
                        "Worker %d picking up %s (%s)",
                        worker_id, job.job_id, job.parsed_url.domain,
                    )
                    result = await self._run_job(job)

                    # Notify user
                    try:
                        await self._on_result(
                            job.user_id, job.chat_id, job.parsed_url, result
                        )
                    except Exception as notify_exc:
                        logger.warning(
                            "Failed to notify user %d: %s",
                            job.user_id, notify_exc,
                        )
            except Exception as exc:
                logger.error(
                    "Worker %d job %s crashed: %s",
                    worker_id, job.job_id, exc, exc_info=True,
                )
            finally:
                # Decrement user's active count and clean up
                self._decrement_user(job.user_id)
                self._queue.task_done()

    async def _run_job(self, job: Job) -> ResolutionResult:
        """Run a single job with a hard total timeout."""
        try:
            result = await asyncio.wait_for(
                resolve_url(job.parsed_url.normalized, job.parsed_url.domain),
                timeout=settings.total_resolution_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("%s hard timeout", job.job_id)
            from resolvers.base import (
                ResolutionMethod,
                ResolutionResult,
                ResolutionStatus,
            )
            result = ResolutionResult.failure(
                ResolutionStatus.TIMEOUT,
                method=ResolutionMethod.NONE,
                error_class="JobHardTimeout",
                error_message=f"Exceeded {settings.total_resolution_timeout_s}s",
                domain=job.parsed_url.domain,
                original_url=job.parsed_url.normalized,
            )

        # Structured log
        log_resolution(
            user_id=job.user_id,
            original_url=job.parsed_url.normalized,
            domain=job.parsed_url.domain,
            status=result.status.value,
            method=result.method.value if result.method else None,
            latency_ms=result.latency_ms,
            error_class=result.error_class,
            target_url=result.target_url,
        )
        return result

    # ---------- Stats ----------

    def _decrement_user(self, user_id: int) -> None:
        active = self._user_active.get(user_id, 0)
        if active <= 1:
            self._user_active.pop(user_id, None)
        else:
            self._user_active[user_id] = active - 1

    def stats(self) -> dict:
        return {
            "queue_size": self._queue.qsize(),
            "active_users": len(self._user_active),
            "workers": len(self._workers),
        }


# ---------- Singleton ----------

_global_queue: QueueManager | None = None


def get_queue_manager(on_result: ResultCallback | None = None) -> QueueManager:
    """
    Singleton accessor. Pass on_result on the first call.
    """
    global _global_queue
    if _global_queue is None:
        if on_result is None:
            raise RuntimeError("QueueManager not initialized; pass on_result=")
        _global_queue = QueueManager(on_result)
        logger.info("QueueManager singleton created")
    return _global_queue