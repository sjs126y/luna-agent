"""CronScheduler — daemon thread + 60s tick + at-most-once semantics."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import weakref

from croniter import croniter

from personal_agent.cron.store import CronStore

logger = logging.getLogger(__name__)


class CronScheduler:
    """Daemon thread with 60s tick. Jobs dispatched via run_coroutine_threadsafe.

    advance_next_run BEFORE run_job → at-most-once semantics (Hermes pattern).
    Independent thread → survives main loop stalls.
    """

    def __init__(self, store: CronStore, submission_port, *, sessions=None) -> None:
        self._store = store
        self._submission_port = weakref.ref(submission_port)
        self._sessions = sessions
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        logger.info("Cron scheduler starting (daemon thread, 60s tick)")
        self._thread = threading.Thread(target=self._tick, daemon=True, name="cron-tick")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Cron scheduler stopped")

    def _tick(self) -> None:
        logger.info("Cron tick thread started")
        while not self._stop_event.wait(timeout=60):
            try:
                self._run_due_jobs()
            except Exception:
                logger.exception("Cron tick failed")

    def _run_due_jobs(self) -> None:
        jobs = self._store.load_all()
        if not jobs:
            return

        now = time.time()
        modified = False

        for job in jobs:
            if not job.enabled:
                continue
            # Calculate next_run on first load
            if job.next_run == 0:
                job.next_run = self._calc_next(job.schedule, now)
                modified = True
                continue
            if now < job.next_run:
                continue

            # ★ advance BEFORE run (at-most-once)
            job.last_run = now
            job.next_run = self._calc_next(job.schedule, now)
            modified = True

            logger.info("Cron job triggered: %s (prompt=%s...)", job.name, job.prompt[:50])

            # Bridge to main loop
            submission_port = self._submission_port()
            if submission_port and self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    lambda j=job: asyncio.ensure_future(self._execute_job(j))
                )

        if modified:
            self._store.save_all(jobs)

    async def _execute_job(self, job) -> None:
        """Run on main loop: send prompt to Agent, deliver result to chat."""
        try:
            submission_port = self._submission_port()
            if submission_port is None:
                return
            from personal_agent.conversation import ResponseMode, SubmissionOrigin, SubmissionRequest
            from personal_agent.models.messages import SessionSource

            user_id = str(job.session_key or "").rpartition(":")[2] or "scheduler"
            source = SessionSource(
                platform=job.platform,
                user_id=user_id,
                user_name="Cron",
                chat_id=job.chat_id,
                chat_type="dm",
            )
            if self._sessions is not None and job.chat_id:
                self._sessions.bind(job.session_key, source)
            request = SubmissionRequest.text(
                session_key=job.session_key,
                text=job.prompt,
                origin=SubmissionOrigin.CRON,
                response_mode=ResponseMode.DELIVER if job.chat_id else ResponseMode.SILENT,
                source=source,
                owner_id=job.job_id,
                metadata={"cron_job_id": job.job_id, "cron_job_name": job.name},
            )
            handle = await submission_port.submit(request)
            outcome = await handle.outcome()
            logger.info("Cron job completed: %s status=%s", job.name, outcome.status.value)
        except Exception:
            logger.exception("Cron job '%s' failed", job.name)

    @staticmethod
    def _calc_next(schedule: str, from_time: float) -> float:
        try:
            cron = croniter(schedule, from_time)
            return cron.get_next(float)
        except Exception:
            return from_time + 3600  # fallback: 1 hour
