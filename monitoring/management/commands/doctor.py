"""Pre-flight checks for a deployment: environment, packages, storage, worker."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from monitoring.models import Camera, DetectionRun


class Command(BaseCommand):
    help = "Check that this machine can run detection reliably."

    def handle(self, *args, **options):
        problems: list[str] = []
        warnings: list[str] = []

        def ok(label: str, detail: str = "") -> None:
            self.stdout.write(self.style.SUCCESS(f"  ok    {label}") + (f"  {detail}" if detail else ""))

        def warn(label: str, detail: str = "") -> None:
            warnings.append(label)
            self.stdout.write(self.style.WARNING(f"  warn  {label}") + (f"  {detail}" if detail else ""))

        def fail(label: str, detail: str = "") -> None:
            problems.append(label)
            self.stdout.write(self.style.ERROR(f"  fail  {label}") + (f"  {detail}" if detail else ""))

        import django

        self.stdout.write(self.style.MIGRATE_HEADING("Configuration"))
        self.stdout.write(f"  Django {django.get_version()}")
        self.stdout.write(f"  DEBUG={settings.DEBUG}  timezone={settings.TIME_ZONE}  device={settings.CHICKEN_DEVICE}")
        if settings.DEBUG:
            warn("DEBUG is enabled", "never run a public deployment with DEBUG=1")
        if not os.environ.get("DJANGO_SECRET_KEY"):
            if settings.DEBUG:
                warn("DJANGO_SECRET_KEY is unset", "sessions reset on restart; set it in .env")
            else:
                fail("DJANGO_SECRET_KEY is unset", "required when DEBUG=0")
        if not settings.ALLOWED_HOSTS or settings.ALLOWED_HOSTS == ["*"]:
            fail("ALLOWED_HOSTS is empty or wildcard", "list the hostnames users will visit")
        if not settings.SECURE_SSL_REDIRECT and not settings.DEBUG:
            warn("TLS redirect disabled", "set DJANGO_BEHIND_TLS_PROXY=1 behind nginx/Caddy")

        self.stdout.write(self.style.MIGRATE_HEADING("Database"))
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            ok("database reachable", connection.settings_dict.get("ENGINE", ""))
            executor = MigrationExecutor(connection)
            unapplied = executor.migration_plan(executor.loader.graph.leaf_nodes())
            if unapplied:
                fail(f"{len(unapplied)} unapplied migration(s)", "run: python manage.py migrate")
            else:
                ok("migrations applied")
        except Exception as exc:  # pragma: no cover - depends on environment
            fail("database unreachable", str(exc))

        self.stdout.write(self.style.MIGRATE_HEADING("Python packages"))
        for module, label in [
            ("torch", "PyTorch"),
            ("torchvision", "torchvision"),
            ("ultralytics", "Ultralytics YOLO"),
            ("cv2", "OpenCV"),
            ("deep_sort_realtime", "DeepSORT"),
            ("numpy", "NumPy"),
            ("imageio_ffmpeg", "imageio-ffmpeg"),
            ("whitenoise", "WhiteNoise"),
            ("argon2", "Argon2"),
        ]:
            try:
                imported = importlib.import_module(module)
            except Exception as exc:
                fail(f"{label} missing", f"{module}: {exc}")
            else:
                version = getattr(imported, "__version__", "")
                ok(label, version)

        try:
            import torch

            ok(
                "torch compute",
                f"cuda_available={torch.cuda.is_available()} threads={torch.get_num_threads()}",
            )
        except Exception:
            pass

        self.stdout.write(self.style.MIGRATE_HEADING("Model and media"))
        weights = Path(settings.CHICKEN_MODEL_PATH)
        if weights.exists():
            ok("chicken weights present", str(weights))
        else:
            warn(
                "chicken weights missing",
                f"{weights} — falls back to yolo11n.pt (COCO bird class). Train with train_model.",
            )
        try:
            import imageio_ffmpeg

            ok("bundled ffmpeg", imageio_ffmpeg.get_ffmpeg_exe())
        except Exception as exc:
            fail("ffmpeg unavailable", f"annotated video export will fail: {exc}")

        for label, root in [
            ("uploads", settings.UPLOAD_ROOT),
            ("outputs", settings.OUTPUT_ROOT),
            ("thumbnails", settings.THUMBNAIL_ROOT),
            ("live frames", settings.LIVE_FRAME_ROOT),
            ("logs", settings.LOG_DIR),
        ]:
            path = Path(root)
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                fail(f"{label} not writable", f"{path}: {exc}")
            else:
                ok(f"{label} writable", str(path))

        self.stdout.write(self.style.MIGRATE_HEADING("Worker"))
        queued = DetectionRun.objects.filter(status=DetectionRun.Status.QUEUED).count()
        running = DetectionRun.objects.filter(status=DetectionRun.Status.RUNNING).count()
        if queued and not running:
            warn(f"{queued} run(s) queued with nothing running", "start: python manage.py run_worker")
        else:
            ok("queue", f"queued={queued} running={running}")
        cameras = Camera.objects.filter(is_active=True).count()
        ok("cameras", f"{cameras} active")

        self.stdout.write("")
        if problems:
            self.stdout.write(self.style.ERROR(f"{len(problems)} problem(s), {len(warnings)} warning(s)."))
            raise SystemExit(1)
        self.stdout.write(
            self.style.SUCCESS(f"Ready. {len(warnings)} warning(s).")
        )
