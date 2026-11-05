"""Application services shared by views and commands."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import Alert, Camera, DetectionRun, VideoAsset, Zone


class ServiceError(RuntimeError):
    """A user-facing action cannot be completed."""


def camera_profile(camera: Camera) -> dict:
    """Detection settings + geometry of a camera, for reuse on uploaded videos."""
    return {
        "confidence": camera.confidence,
        "iou": camera.iou,
        "imgsz": camera.imgsz,
        "frame_skip": camera.frame_skip,
        "min_track_hits": camera.min_track_hits,
        "roi": camera.roi,
        "roi_only": camera.roi_only,
        "counting_line": camera.counting_line,
        "invert_line": camera.invert_line,
        "zones": list(
            Zone.objects.filter(camera=camera, is_active=True).values("name", "kind", "polygon")
        ),
    }


def active_run_for_camera(camera: Camera) -> DetectionRun | None:
    return (
        DetectionRun.objects.filter(
            camera=camera,
            status__in=[DetectionRun.Status.QUEUED, DetectionRun.Status.RUNNING],
        )
        .order_by("-created_at")
        .first()
    )


@transaction.atomic
def start_camera_run(camera: Camera, user) -> DetectionRun:
    if not camera.stream_url:
        raise ServiceError("Add an RTSP/HTTP stream URL before starting this camera.")
    if not camera.is_active:
        raise ServiceError("This camera is disabled. Enable it in its settings first.")
    existing = active_run_for_camera(camera)
    if existing is not None:
        raise ServiceError("This camera is already being monitored.")

    run = DetectionRun.objects.create(
        camera=camera,
        source_type=DetectionRun.SourceType.CAMERA,
        title=f"{camera.name} — live",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    Camera.objects.filter(pk=camera.pk).update(status=Camera.Status.STARTING, last_error="")
    return run


def stop_camera_run(camera: Camera) -> DetectionRun | None:
    run = active_run_for_camera(camera)
    if run is None:
        return None
    DetectionRun.objects.filter(pk=run.pk).update(cancel_requested=True)
    return run


@transaction.atomic
def create_video_run(video: VideoAsset, user, *, title: str = "", profile: dict | None = None) -> DetectionRun:
    run = DetectionRun.objects.create(
        source_type=DetectionRun.SourceType.VIDEO,
        video=video,
        title=(title or video.original_name)[:160],
        created_by=user if getattr(user, "is_authenticated", False) else None,
        config={"profile": profile} if profile else {},
    )
    return run


def retry_run(run: DetectionRun, user) -> DetectionRun:
    if run.source_type == DetectionRun.SourceType.VIDEO:
        if run.video is None:
            raise ServiceError("The original video is no longer available.")
        return DetectionRun.objects.create(
            source_type=run.source_type,
            video=run.video,
            title=f"{run.title or 'Run'} (retry)"[:160],
            created_by=user,
            config={"profile": (run.config or {}).get("profile")},
        )
    if run.camera is None:
        raise ServiceError("The camera for this run no longer exists.")
    return start_camera_run(run.camera, user)


def acknowledge_alert(alert: Alert, user) -> Alert:
    if alert.is_acknowledged:
        return alert
    alert.is_acknowledged = True
    alert.acknowledged_by = user
    alert.acknowledged_at = timezone.now()
    alert.save(update_fields=["is_acknowledged", "acknowledged_by", "acknowledged_at"])
    return alert


def latest_live_sample(run: DetectionRun):
    return run.samples.order_by("-timestamp_seconds").first()
