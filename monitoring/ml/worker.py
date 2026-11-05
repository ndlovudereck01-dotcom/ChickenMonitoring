"""Database-backed job worker.

Queued runs are claimed with an atomic conditional UPDATE, so multiple threads (or
even several worker processes) never pick the same run.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from monitoring.models import Camera, DetectionRun

from .pipeline import execute_run

logger = logging.getLogger("monitoring.ml.worker")


def claim_next_run() -> DetectionRun | None:
    """Atomically move the oldest queued run to RUNNING, or return None."""
    close_old_connections()
    while True:
        candidate_id = (
            DetectionRun.objects.filter(status=DetectionRun.Status.QUEUED)
            .order_by("created_at")
            .values_list("pk", flat=True)
            .first()
        )
        if candidate_id is None:
            return None
        claimed = DetectionRun.objects.filter(
            pk=candidate_id, status=DetectionRun.Status.QUEUED
        ).update(status=DetectionRun.Status.RUNNING, started_at=timezone.now(), progress=0)
        if claimed:
            return DetectionRun.objects.select_related("camera", "video").get(pk=candidate_id)
        # Another thread won the race; look for the next queued run.


def reconcile_after_crash() -> int:
    """Mark runs left RUNNING by a dead worker as failed."""
    now = timezone.now()
    count = DetectionRun.objects.filter(status=DetectionRun.Status.RUNNING).update(
        status=DetectionRun.Status.FAILED,
        finished_at=now,
        error_message="Processing was interrupted because the worker restarted.",
    )
    Camera.objects.exclude(status=Camera.Status.OFFLINE).update(
        status=Camera.Status.OFFLINE, last_error="Worker restarted."
    )
    if count:
        logger.warning("Reconciled %s stale run(s) after worker restart", count)
    return count


class Worker:
    def __init__(self, concurrency: int | None = None, poll_seconds: float | None = None) -> None:
        self.concurrency = max(1, int(concurrency or settings.CHICKEN_WORKER_CONCURRENCY))
        self.poll_seconds = float(poll_seconds or settings.CHICKEN_WORKER_POLL_SECONDS)
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency, thread_name_prefix="detection-run"
        )
        self._futures: dict = {}

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> int:
        return len(self._futures)

    def _reap(self) -> None:
        for future in [f for f in self._futures if f.done()]:
            run_id = self._futures.pop(future)
            try:
                future.result()
            except Exception:  # pragma: no cover - execute_run already swallows
                logger.exception("Run %s raised from the worker pool", run_id)

    def run_once(self) -> bool:
        """Claim and execute a single run synchronously. Returns False when idle."""
        run = claim_next_run()
        if run is None:
            return False
        execute_run(run.pk)
        return True

    def run_forever(self, max_iterations: int | None = None) -> None:
        logger.info(
            "Detection worker started (concurrency=%s, poll=%ss)", self.concurrency, self.poll_seconds
        )
        reconcile_after_crash()
        iterations = 0
        try:
            while not self._stop.is_set():
                self._reap()
                claimed = False
                while len(self._futures) < self.concurrency:
                    run = claim_next_run()
                    if run is None:
                        break
                    logger.info("Claimed run %s", run.pk)
                    future = self._executor.submit(execute_run, run.pk)
                    self._futures[future] = run.pk
                    claimed = True
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                if not claimed:
                    self._stop.wait(self.poll_seconds)
        finally:
            logger.info("Worker stopping, waiting for %s active run(s)", len(self._futures))
            self._executor.shutdown(wait=True)
            logger.info("Worker stopped")

    def drain(self) -> None:
        """Wait for all in-flight runs (used by tests)."""
        self._executor.shutdown(wait=True)
