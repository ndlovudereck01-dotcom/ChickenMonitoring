"""Domain model: cameras, footage, detection runs, tracks and counts."""

from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.utils import timezone

from .validators import validate_line, validate_polygon


def _upload_path(prefix: str, filename: str) -> str:
    extension = Path(filename).suffix.lower()[:10]
    stamp = timezone.now()
    return f"private/{prefix}/{stamp:%Y/%m}/{uuid.uuid4().hex}{extension}"


def video_upload_to(instance, filename: str) -> str:
    return _upload_path("videos", filename)


def output_upload_to(instance, filename: str) -> str:
    return _upload_path("annotated", filename)


def thumbnail_upload_to(instance, filename: str) -> str:
    return _upload_path("thumbnails", filename)


class Camera(models.Model):
    """A fixed camera (RTSP/HTTP feed) watching one poultry house."""

    class Status(models.TextChoices):
        OFFLINE = "offline", "Offline"
        STARTING = "starting", "Starting"
        ONLINE = "online", "Online"
        ERROR = "error", "Error"

    name = models.CharField(max_length=120, unique=True)
    location = models.CharField(max_length=160, blank=True)
    stream_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="RTSP/HTTP stream URL. Leave empty for a demo camera using an uploaded video.",
    )
    is_active = models.BooleanField(default=True)

    # Per-camera detector tuning (defaults follow settings.py).
    confidence = models.FloatField(default=0.35, help_text="Minimum detection confidence (0-1).")
    iou = models.FloatField(default=0.50, help_text="NMS IoU threshold (0-1).")
    imgsz = models.PositiveIntegerField(default=640, help_text="Inference resolution.")
    frame_skip = models.PositiveIntegerField(
        default=2, help_text="Run detection on every Nth frame (higher = faster)."
    )
    min_track_hits = models.PositiveIntegerField(
        default=5,
        help_text="Frames a track must be seen before it is counted (filters flicker).",
    )

    # Geometry, normalised to 0-1 so it is resolution independent.
    roi = models.JSONField(
        null=True,
        blank=True,
        validators=[validate_polygon],
        help_text="Optional polygon of the monitored area: [[x,y], ...].",
    )
    roi_only = models.BooleanField(
        default=False, help_text="Ignore and do not count detections outside the ROI."
    )
    counting_line = models.JSONField(
        null=True,
        blank=True,
        validators=[validate_line],
        help_text="Optional counting line [[x1,y1],[x2,y2]] used for IN/OUT events.",
    )
    invert_line = models.BooleanField(
        default=False, help_text="Swap the IN and OUT directions of the counting line."
    )

    occupancy_alert_threshold = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Raise an overcrowding alert when more birds than this are visible.",
    )

    notes = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OFFLINE)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cameras",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["status", "is_active"])]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        super().clean()
        validate_polygon(self.roi)
        validate_line(self.counting_line)
        if self.counting_line and len(self.counting_line) != 2:
            raise models.ValidationError("A counting line needs exactly two points.")

    @property
    def active_run(self):
        return (
            self.runs.filter(status=DetectionRun.Status.RUNNING)
            .order_by("-started_at")
            .first()
        )

    @property
    def masked_stream_url(self) -> str:
        """Hide credentials embedded in a stream URL before showing it in the UI."""
        url = self.stream_url or ""
        if "://" in url and "@" in url:
            scheme, rest = url.split("://", 1)
            return f"{scheme}://***@{rest.split('@', 1)[1]}"
        return url

    @property
    def is_live(self) -> bool:
        return self.status == self.Status.ONLINE


class Zone(models.Model):
    """A named area inside a camera view (feeder, drinker, door...)."""

    class Kind(models.TextChoices):
        FEEDER = "feeder", "Feeder"
        WATER = "water", "Drinker"
        ROOST = "roost", "Roost"
        DOOR = "door", "Door / exit"
        CUSTOM = "custom", "Custom"

    camera = models.ForeignKey(Camera, on_delete=models.CASCADE, related_name="zones")
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.CUSTOM)
    polygon = models.JSONField(validators=[validate_polygon])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("camera", "name")]

    def __str__(self) -> str:
        return f"{self.camera.name} / {self.name}"


class VideoAsset(models.Model):
    """An uploaded video file that has been probed and fingerprinted."""

    file = models.FileField(upload_to=video_upload_to)
    original_name = models.CharField(max_length=255)
    size_bytes = models.BigIntegerField(default=0)
    duration_seconds = models.FloatField(default=0)
    fps = models.FloatField(default=0)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="videos",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.original_name

    @property
    def duration_display(self) -> str:
        total = int(self.duration_seconds or 0)
        return f"{total // 60:d}:{total % 60:02d}"


class DetectionRun(models.Model):
    """One processing job: an uploaded video or a live camera session."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class SourceType(models.TextChoices):
        VIDEO = "video", "Uploaded video"
        CAMERA = "camera", "Live camera"

    title = models.CharField(max_length=160, blank=True)
    source_type = models.CharField(max_length=10, choices=SourceType.choices)
    camera = models.ForeignKey(
        Camera, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs"
    )
    video = models.ForeignKey(
        VideoAsset, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs"
    )

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    progress = models.PositiveSmallIntegerField(default=0)
    frames_total = models.PositiveIntegerField(default=0)
    frames_processed = models.PositiveIntegerField(default=0)
    processing_fps = models.FloatField(default=0)

    unique_birds = models.PositiveIntegerField(default=0)
    peak_occupancy = models.PositiveIntegerField(default=0)
    average_occupancy = models.FloatField(default=0)
    line_in = models.PositiveIntegerField(default=0)
    line_out = models.PositiveIntegerField(default=0)
    sample_count = models.PositiveIntegerField(default=0)

    detector_model = models.CharField(max_length=120, blank=True)
    device = models.CharField(max_length=40, blank=True)
    config = models.JSONField(default=dict, blank=True)

    output_video = models.FileField(upload_to=output_upload_to, null=True, blank=True)
    thumbnail = models.FileField(upload_to=thumbnail_upload_to, null=True, blank=True)
    error_message = models.TextField(blank=True)
    cancel_requested = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="runs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["source_type", "created_at"]),
        ]

    def __str__(self) -> str:
        return self.title or f"Run #{self.pk} ({self.get_source_type_display()})"

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("monitoring:run_detail", args=[self.pk])

    def clean(self):
        super().clean()
        if self.source_type == self.SourceType.VIDEO and not self.video_id:
            raise models.ValidationError("Video runs require an uploaded video.")
        if self.source_type == self.SourceType.CAMERA and not self.camera_id:
            raise models.ValidationError("Camera runs require a camera.")

    @property
    def is_finished(self) -> bool:
        return self.status in {
            self.Status.COMPLETED,
            self.Status.FAILED,
            self.Status.CANCELLED,
        }

    @property
    def elapsed_display(self) -> str:
        end = self.finished_at or timezone.now()
        start = self.started_at or self.created_at
        seconds = max(0, int((end - start).total_seconds()))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes:02d}m"
        return f"{minutes}m {secs:02d}s"

    @property
    def duration_seconds(self) -> float:
        if self.video_id:
            return float(self.video.duration_seconds or 0)
        return 0.0


class BirdTrack(models.Model):
    """A unique bird trajectory produced by the tracker for one run."""

    run = models.ForeignKey(DetectionRun, on_delete=models.CASCADE, related_name="tracks")
    track_id = models.PositiveIntegerField()
    first_frame = models.PositiveIntegerField(default=0)
    last_frame = models.PositiveIntegerField(default=0)
    first_seen_seconds = models.FloatField(default=0)
    last_seen_seconds = models.FloatField(default=0)
    frames_seen = models.PositiveIntegerField(default=0)
    confidence = models.FloatField(default=0)
    is_counted = models.BooleanField(default=False)
    last_bbox = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["track_id"]
        unique_together = [("run", "track_id")]
        indexes = [models.Index(fields=["run", "is_counted"])]

    def __str__(self) -> str:
        return f"Track {self.track_id} of run {self.run_id}"

    @property
    def visible_seconds(self) -> float:
        return max(0.0, self.last_seen_seconds - self.first_seen_seconds)


class CountSample(models.Model):
    """A periodic snapshot of the population inside the frame."""

    run = models.ForeignKey(DetectionRun, on_delete=models.CASCADE, related_name="samples")
    frame_index = models.PositiveIntegerField()
    timestamp_seconds = models.FloatField()
    occupancy = models.PositiveIntegerField(default=0)
    unique_total = models.PositiveIntegerField(default=0)
    line_in = models.PositiveIntegerField(default=0)
    line_out = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["timestamp_seconds"]
        indexes = [models.Index(fields=["run", "timestamp_seconds"])]


class ZoneStatistic(models.Model):
    """Aggregated occupancy of one zone for one run."""

    run = models.ForeignKey(DetectionRun, on_delete=models.CASCADE, related_name="zone_stats")
    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.SET_NULL)
    zone_name = models.CharField(max_length=80)
    zone_kind = models.CharField(max_length=16, blank=True)
    peak_occupancy = models.PositiveIntegerField(default=0)
    average_occupancy = models.FloatField(default=0)

    class Meta:
        ordering = ["-peak_occupancy"]


class Alert(models.Model):
    """Operational alert raised by the pipeline or by the worker."""

    class Kind(models.TextChoices):
        OVERCROWDING = "overcrowding", "Overcrowding"
        LOW_ACTIVITY = "low_activity", "Low activity"
        CAMERA_OFFLINE = "camera_offline", "Camera offline"
        RUN_FAILED = "run_failed", "Processing failed"
        STOCK_ESCAPE = "stock_escape", "Net outflow"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    camera = models.ForeignKey(
        Camera, null=True, blank=True, on_delete=models.CASCADE, related_name="alerts"
    )
    run = models.ForeignKey(
        DetectionRun, null=True, blank=True, on_delete=models.CASCADE, related_name="alerts"
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.WARNING)
    message = models.CharField(max_length=300)
    metadata = models.JSONField(default=dict, blank=True)
    is_acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["is_acknowledged", "created_at"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} — {self.message}"


class AuditLog(models.Model):
    """Append-only record of security-relevant actions."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["action", "created_at"])]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action}"
