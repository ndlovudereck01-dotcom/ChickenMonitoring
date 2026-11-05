"""Housekeeping: retire old samples, alerts, audit rows and optional footage."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from monitoring.models import Alert, AuditLog, CountSample, DetectionRun, VideoAsset


class Command(BaseCommand):
    help = "Delete old time-series data so the database stays small and fast."

    def add_arguments(self, parser):
        parser.add_argument("--samples-days", type=int, default=30)
        parser.add_argument("--alerts-days", type=int, default=90)
        parser.add_argument("--audit-days", type=int, default=365)
        parser.add_argument(
            "--runs-days",
            type=int,
            default=0,
            help="Also delete finished runs (and their files) older than N days. 0 disables.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        now = timezone.now()
        dry_run = options["dry_run"]

        thresholds = {
            "samples": now - timedelta(days=options["samples_days"]),
            "alerts": now - timedelta(days=options["alerts_days"]),
            "audit": now - timedelta(days=options["audit_days"]),
        }

        sample_qs = CountSample.objects.filter(created_at__lt=thresholds["samples"])
        alert_qs = Alert.objects.filter(is_acknowledged=True, created_at__lt=thresholds["alerts"])
        audit_qs = AuditLog.objects.filter(created_at__lt=thresholds["audit"])

        self.stdout.write(f"Samples to delete: {sample_qs.count()}")
        self.stdout.write(f"Acknowledged alerts to delete: {alert_qs.count()}")
        self.stdout.write(f"Audit entries to delete: {audit_qs.count()}")

        run_qs = DetectionRun.objects.none()
        if options["runs_days"] > 0:
            run_qs = DetectionRun.objects.filter(
                created_at__lt=now - timedelta(days=options["runs_days"]),
                status__in=[
                    DetectionRun.Status.COMPLETED,
                    DetectionRun.Status.FAILED,
                    DetectionRun.Status.CANCELLED,
                ],
            )
            self.stdout.write(f"Finished runs to delete: {run_qs.count()}")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: nothing was deleted."))
            return

        for run in run_qs:
            for field in ("output_video", "thumbnail"):
                file_field = getattr(run, field)
                if file_field and Path(file_field.path).exists():
                    Path(file_field.path).unlink(missing_ok=True)
            run.delete()
        for video in VideoAsset.objects.exclude(runs__isnull=False):
            if video.file and Path(video.file.path).exists():
                Path(video.file.path).unlink(missing_ok=True)
            video.delete()

        sample_qs.delete()
        alert_qs.delete()
        audit_qs.delete()
        self.stdout.write(self.style.SUCCESS("Prune complete."))
