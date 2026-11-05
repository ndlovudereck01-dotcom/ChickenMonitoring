"""Process detection jobs from the queue."""

from __future__ import annotations

import signal
import sys

from django.core.management.base import BaseCommand

from monitoring.ml.worker import Worker, reconcile_after_crash


class Command(BaseCommand):
    help = "Run the detection worker that processes queued video and camera runs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--concurrency",
            type=int,
            default=None,
            help="Number of runs processed in parallel (default: CHICKEN_WORKER_CONCURRENCY).",
        )
        parser.add_argument(
            "--once", action="store_true", help="Process a single queued run and exit."
        )
        parser.add_argument(
            "--reconcile",
            action="store_true",
            help="Mark runs left running by a previous worker as failed, then exit.",
        )

    def handle(self, *args, **options):
        if options["reconcile"]:
            count = reconcile_after_crash()
            self.stdout.write(self.style.SUCCESS(f"Reconciled {count} stale run(s)."))
            return

        worker = Worker(concurrency=options.get("concurrency"))

        if options["once"]:
            processed = worker.run_once()
            worker.drain()
            if processed:
                self.stdout.write(self.style.SUCCESS("Processed one run."))
            else:
                self.stdout.write("No queued runs.")
            return

        def _stop(signum, frame):  # noqa: ARG001
            self.stdout.write("Stopping worker…")
            worker.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _stop)
            except ValueError:  # not running in the main thread
                pass

        try:
            worker.run_forever()
        except KeyboardInterrupt:  # pragma: no cover
            worker.stop()
            sys.exit(0)
        self.stdout.write(self.style.SUCCESS("Worker stopped."))
