from __future__ import annotations

import asyncio
from contextlib import suppress

from .database import Database
from .errors import DocumentProcessingError, ProcessingStopped
from .ingestion import PdfIngestionService
from .schemas import JobStatus


class LocalJobRunner:
    """One durable in-process worker. Jobs survive a restart through SQLite checkpoints."""

    def __init__(self, database: Database, ingestion: PdfIngestionService) -> None:
        self.database = database
        self.ingestion = ingestion
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active_job_id: str | None = None

    async def start(self) -> None:
        self.database.recover_incomplete_work()
        self._task = asyncio.create_task(self._run(), name="factualize-local-job-runner")
        self.notify()

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def notify(self) -> None:
        self._wake_event.set()

    def is_processing(self, job_id: str) -> bool:
        return self._active_job_id == job_id

    async def _run(self) -> None:
        while True:
            job = self.database.next_queued_job()
            if job is None:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=2)
                except TimeoutError:
                    pass
                continue
            try:
                self._active_job_id = job["id"]
                await asyncio.to_thread(self.ingestion.process, job["id"])
            except ProcessingStopped:
                self.database.append_history(
                    "job_stopped",
                    job["id"],
                    {"job_id": job["id"], "message": "Stopped by the user."},
                    set_id=job.get("set_id"),
                )
            except DocumentProcessingError as error:
                self.database.update_job(
                    job["id"],
                    status=JobStatus.TERMINAL_FAILURE,
                    last_error={"type": type(error).__name__, "message": str(error)},
                    progress_detail="Document needs attention.",
                )
                self.database.append_history(
                    "job_terminal_failure",
                    job["id"],
                    {"job_id": job["id"], "message": str(error)},
                    set_id=job.get("set_id"),
                )
            except Exception as error:  # Protect the queue; unexpected errors remain visible.
                self.database.update_job(
                    job["id"],
                    status=JobStatus.NEEDS_ATTENTION,
                    last_error={"type": type(error).__name__, "message": str(error)},
                    progress_detail="Unexpected processing error. Inspect history before retrying.",
                )
                self.database.append_history(
                    "job_unexpected_failure",
                    job["id"],
                    {"job_id": job["id"], "message": str(error)},
                    set_id=job.get("set_id"),
                )
            finally:
                self._active_job_id = None
