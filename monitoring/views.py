"""HTTP views: dashboards, camera management, runs, live streams and reports."""

from __future__ import annotations

import csv
import json
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required, login_required
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Avg, Count, Max, OuterRef, Subquery, Sum
from django.db.models.functions import TruncDate
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import CameraForm, GeometryForm, VideoUploadForm, ZoneForm
from .ml.live import BOUNDARY, live_frame_age, mjpeg_stream, read_live_frame
from .ml.media import MediaError, file_sha256, probe_video
from .models import Alert, AuditLog, BirdTrack, Camera, CountSample, DetectionRun, VideoAsset, Zone
from .security import admin_required, manager_required
from .services import (
    ServiceError,
    acknowledge_alert,
    active_run_for_camera,
    camera_profile,
    create_video_run,
    latest_live_sample,
    retry_run,
    start_camera_run,
    stop_camera_run,
)
from .utils import audit, downsample, humanize_seconds

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_filename(name: str, fallback: str = "file") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", (name or "")).strip()[:120]
    return cleaned or fallback


def range_file_response(request, path: Path, content_type: str, filename: str = "") -> HttpResponse:
    """Serve a private file with HTTP Range support so video seeking works."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise Http404("File is no longer available.") from exc

    range_header = (request.headers.get("Range") or "").strip()
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
    if match and (match.group(1) or match.group(2)) and size > 0:
        if match.group(1):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else size - 1
        else:
            suffix = int(match.group(2))
            start = max(0, size - suffix)
            end = size - 1
        if start >= size or start > end:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        end = min(end, size - 1)
        length = end - start + 1
        response = StreamingHttpResponse(
            _file_chunks(path, start, length), status=206, content_type=content_type
        )
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = str(length)
    else:
        response = FileResponse(open(path, "rb"), content_type=content_type)

    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, max-age=300"
    response["Content-Disposition"] = f'inline; filename="{_safe_filename(filename)}"'
    return response


def _file_chunks(path: Path, start: int, length: int, chunk_size: int = 256 * 1024):
    remaining = length
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            data = handle.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def _active_runs_with_occupancy():
    latest_occupancy = (
        CountSample.objects.filter(run=OuterRef("pk"))
        .order_by("-timestamp_seconds")
        .values("occupancy")[:1]
    )
    latest_updated = (
        CountSample.objects.filter(run=OuterRef("pk"))
        .order_by("-timestamp_seconds")
        .values("timestamp_seconds")[:1]
    )
    return (
        DetectionRun.objects.filter(status=DetectionRun.Status.RUNNING)
        .select_related("camera", "video")
        .annotate(live_occupancy=Subquery(latest_occupancy), live_ts=Subquery(latest_updated))
    )


def _cameras_with_last_run():
    latest = DetectionRun.objects.filter(camera=OuterRef("pk")).order_by("-created_at")
    return Camera.objects.annotate(
        last_run_id=Subquery(latest.values("id")[:1]),
        last_thumb=Subquery(latest.values("thumbnail")[:1]),
        last_unique=Subquery(latest.values("unique_birds")[:1]),
    )


def _page(request, queryset, per_page: int = 25):
    paginator = Paginator(queryset, per_page)
    return paginator.get_page(request.GET.get("page"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@login_required
@require_GET
def dashboard(request):
    now = timezone.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    cameras = list(_cameras_with_last_run())
    active_runs = list(_active_runs_with_occupancy())
    runs_by_camera = {run.camera_id: run for run in active_runs if run.camera_id}

    live_birds = sum(run.live_occupancy or 0 for run in active_runs)
    today_runs = DetectionRun.objects.filter(created_at__gte=start_of_day)
    today_counts = today_runs.aggregate(
        birds=Sum("unique_birds"),
        runs=Count("id"),
        peak=Max("peak_occupancy"),
    )

    open_alerts = Alert.objects.filter(is_acknowledged=False)
    recent_runs = (
        DetectionRun.objects.select_related("camera", "video").order_by("-created_at")[:6]
    )
    recent_alerts = open_alerts.select_related("camera")[:5]

    # Population trend for live cameras (last 10 minutes of samples).
    trend_series = []
    for run in active_runs:
        samples = list(
            run.samples.order_by("-timestamp_seconds")[:180]
        )
        samples.reverse()
        if samples:
            trend_series.append(
                {
                    "label": run.camera.name if run.camera else run.title,
                    "points": [
                        {"t": round(sample.timestamp_seconds, 2), "v": sample.occupancy}
                        for sample in samples
                    ],
                }
            )

    context = {
        "cameras": cameras,
        "runs_by_camera": runs_by_camera,
        "live_birds": live_birds,
        "online_cameras": sum(1 for camera in cameras if camera.status == Camera.Status.ONLINE),
        "camera_total": len(cameras),
        "today_birds": today_counts["birds"] or 0,
        "today_runs": today_counts["runs"] or 0,
        "today_peak": today_counts["peak"] or 0,
        "open_alerts": open_alerts.count(),
        "active_runs": len(active_runs),
        "recent_runs": recent_runs,
        "recent_alerts": recent_alerts,
        "trend_series": trend_series,
        "trend_json": json.dumps(trend_series),
    }
    return render(request, "monitoring/dashboard.html", context)


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------


@login_required
@require_GET
def camera_list(request):
    cameras = list(_cameras_with_last_run())
    active = {run.camera_id: run for run in _active_runs_with_occupancy() if run.camera_id}
    last_runs = {}
    for camera in cameras:
        last = (
            DetectionRun.objects.filter(camera=camera)
            .order_by("-created_at")
            .only("id", "status", "unique_birds", "created_at", "thumbnail")
            .first()
        )
        if last:
            last_runs[camera.pk] = last
    return render(
        request,
        "monitoring/cameras.html",
        {"cameras": cameras, "active_runs": active, "last_runs": last_runs},
    )


@login_required
@require_GET
def camera_detail(request, pk: int):
    camera = get_object_or_404(Camera, pk=pk)
    active_run = active_run_for_camera(camera)
    live_sample = latest_live_sample(active_run) if active_run else None
    runs = (
        DetectionRun.objects.filter(camera=camera)
        .select_related("camera")
        .order_by("-created_at")[:8]
    )
    zones = camera.zones.all()
    can_manage = request.user.is_manager
    context = {
        "camera": camera,
        "active_run": active_run,
        "live_sample": live_sample,
        "runs": runs,
        "zones": zones,
        "can_manage": can_manage,
        "frame_age": live_frame_age(camera.pk),
        "geometry": {
            "roi": camera.roi,
            "counting_line": camera.counting_line,
            "invert_line": camera.invert_line,
            "roi_only": camera.roi_only,
        },
        "zone_form": ZoneForm(),
    }
    return render(request, "monitoring/camera_detail.html", context)


@login_required
@manager_required
@require_http_methods(["GET", "POST"])
def camera_create(request):
    if request.method == "POST":
        form = CameraForm(request.POST)
        if form.is_valid():
            camera: Camera = form.save(commit=False)
            camera.created_by = request.user
            camera.save()
            audit(request, "camera.create", target=camera, detail=camera.name)
            messages.success(request, f"Camera “{camera.name}” created.")
            return redirect("monitoring:camera_detail", pk=camera.pk)
    else:
        form = CameraForm()
    return render(
        request, "monitoring/camera_form.html", {"form": form, "camera": None, "is_new": True}
    )


@login_required
@manager_required
@require_http_methods(["GET", "POST"])
def camera_edit(request, pk: int):
    camera = get_object_or_404(Camera, pk=pk)
    if request.method == "POST":
        form = CameraForm(request.POST, instance=camera)
        if form.is_valid():
            form.save()
            audit(request, "camera.update", target=camera, detail=camera.name)
            messages.success(request, "Camera settings saved.")
            return redirect("monitoring:camera_detail", pk=camera.pk)
    else:
        form = CameraForm(instance=camera)
    return render(
        request, "monitoring/camera_form.html", {"form": form, "camera": camera, "is_new": False}
    )


@login_required
@manager_required
@require_POST
def camera_start(request, pk: int):
    camera = get_object_or_404(Camera, pk=pk)
    try:
        run = start_camera_run(camera, request.user)
    except ServiceError as exc:
        messages.error(request, str(exc))
    else:
        audit(request, "camera.start", target=camera, detail=f"run={run.pk}")
        messages.success(request, "Camera queued — the worker will connect shortly.")
    return redirect("monitoring:camera_detail", pk=camera.pk)


@login_required
@manager_required
@require_POST
def camera_stop(request, pk: int):
    camera = get_object_or_404(Camera, pk=pk)
    run = stop_camera_run(camera)
    if run:
        audit(request, "camera.stop", target=camera, detail=f"run={run.pk}")
        messages.success(request, "Stopping the camera session…")
    else:
        messages.info(request, "This camera is not running.")
    return redirect("monitoring:camera_detail", pk=camera.pk)


@login_required
@manager_required
@require_POST
def camera_geometry(request, pk: int):
    camera = get_object_or_404(Camera, pk=pk)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON payload."}, status=400)

    form = GeometryForm(payload)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get_json_data()}, status=400)

    camera.roi = form.cleaned_data.get("roi")
    camera.counting_line = form.cleaned_data.get("counting_line")
    camera.invert_line = form.cleaned_data.get("invert_line", False)
    camera.roi_only = form.cleaned_data.get("roi_only", False)
    camera.full_clean(exclude=["created_by"])
    camera.save(update_fields=["roi", "counting_line", "invert_line", "roi_only", "updated_at"])
    audit(request, "camera.geometry", target=camera, detail=camera.name)
    return JsonResponse({"ok": True, "roi": camera.roi, "counting_line": camera.counting_line})


@login_required
@manager_required
@require_POST
def zone_create(request, pk: int):
    camera = get_object_or_404(Camera, pk=pk)
    form = ZoneForm(request.POST)
    if form.is_valid():
        zone: Zone = form.save(commit=False)
        zone.camera = camera
        zone.save()
        audit(request, "zone.create", target=zone, detail=f"{camera.name}/{zone.name}")
        messages.success(request, f"Zone “{zone.name}” added.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")
    return redirect("monitoring:camera_detail", pk=camera.pk)


@login_required
@manager_required
@require_POST
def zone_delete(request, pk: int):
    zone = get_object_or_404(Zone, pk=pk)
    camera_id = zone.camera_id
    audit(request, "zone.delete", target=zone, detail=zone.name)
    zone.delete()
    messages.success(request, "Zone removed.")
    return redirect("monitoring:camera_detail", pk=camera_id)


# ---------------------------------------------------------------------------
# Uploads and runs
# ---------------------------------------------------------------------------


@login_required
@manager_required
@require_http_methods(["GET", "POST"])
def video_upload(request):
    if request.method == "POST":
        form = VideoUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.cleaned_data["video"]
            asset = VideoAsset(
                original_name=_safe_filename(upload.name, "video.mp4")[:255],
                size_bytes=upload.size,
                uploaded_by=request.user,
                file=upload,
            )
            asset.save()
            try:
                probe = probe_video(asset.file.path)
            except MediaError as exc:
                asset.file.delete(save=False)
                asset.delete()
                form.add_error("video", str(exc))
            else:
                if probe.duration_seconds > settings.CHICKEN_MAX_VIDEO_SECONDS:
                    asset.file.delete(save=False)
                    asset.delete()
                    form.add_error(
                        "video",
                        f"Video is longer than the {settings.CHICKEN_MAX_VIDEO_SECONDS // 60} minute limit.",
                    )
                else:
                    asset.duration_seconds = probe.duration_seconds
                    asset.fps = probe.fps
                    asset.width = probe.width
                    asset.height = probe.height
                    asset.sha256 = file_sha256(asset.file.path)
                    asset.save(
                        update_fields=[
                            "duration_seconds",
                            "fps",
                            "width",
                            "height",
                            "sha256",
                        ]
                    )
                    profile = None
                    camera = form.cleaned_data.get("camera_profile")
                    if camera is not None:
                        profile = camera_profile(camera)
                    run = create_video_run(
                        asset,
                        request.user,
                        title=form.cleaned_data.get("title", ""),
                        profile=profile,
                    )
                    audit(request, "run.create", target=run, detail=asset.original_name)
                    messages.success(
                        request,
                        "Upload complete. Detection is queued — this page updates as it runs.",
                    )
                    return redirect("monitoring:run_detail", pk=run.pk)
    else:
        form = VideoUploadForm()
    return render(request, "monitoring/upload.html", {"form": form})


@login_required
@require_GET
def run_list(request):
    runs = DetectionRun.objects.select_related("camera", "video").order_by("-created_at")
    status = request.GET.get("status", "")
    source = request.GET.get("source", "")
    query = request.GET.get("q", "").strip()
    if status in DetectionRun.Status.values:
        runs = runs.filter(status=status)
    if source in DetectionRun.SourceType.values:
        runs = runs.filter(source_type=source)
    if query:
        runs = runs.filter(title__icontains=query)
    page = _page(request, runs, per_page=20)
    return render(
        request,
        "monitoring/runs.html",
        {
            "page": page,
            "status_choices": DetectionRun.Status.choices,
            "source_choices": DetectionRun.SourceType.choices,
            "filters": {"status": status, "source": source, "q": query},
        },
    )


@login_required
@require_GET
def run_detail(request, pk: int):
    run = get_object_or_404(
        DetectionRun.objects.select_related("camera", "video", "created_by"), pk=pk
    )
    samples = list(
        run.samples.order_by("timestamp_seconds").values(
            "timestamp_seconds", "occupancy", "unique_total", "line_in", "line_out"
        )
    )
    chart_points = downsample(samples, 240)
    tracks = run.tracks.order_by("track_id")
    tracks_page = _page(request, tracks, per_page=60)
    zone_stats = run.zone_stats.all()
    output_exists = bool(run.output_video) and Path(run.output_video.path).exists()

    context = {
        "run": run,
        "samples": samples,
        "chart_points": chart_points,
        "tracks_page": tracks_page,
        "zone_stats": zone_stats,
        "output_exists": output_exists,
        "elapsed": humanize_seconds(
            ((run.finished_at or timezone.now()) - run.started_at).total_seconds()
            if run.started_at
            else 0
        ),
        "alerts": run.alerts.all()[:10],
        "run_config": json.dumps(run.config or {}, indent=2, sort_keys=True, default=str),
        "can_manage": request.user.is_manager,
        "can_delete": request.user.is_admin_role,
    }
    return render(request, "monitoring/run_detail.html", context)


@login_required
@manager_required
@require_POST
def run_cancel(request, pk: int):
    run = get_object_or_404(DetectionRun, pk=pk)
    if run.is_finished:
        messages.info(request, "This run has already finished.")
    elif run.status == DetectionRun.Status.QUEUED:
        DetectionRun.objects.filter(pk=run.pk, status=DetectionRun.Status.QUEUED).update(
            status=DetectionRun.Status.CANCELLED,
            finished_at=timezone.now(),
            error_message="Cancelled before processing started.",
        )
        messages.success(request, "Queued run cancelled.")
        audit(request, "run.cancel", target=run)
    else:
        DetectionRun.objects.filter(pk=run.pk).update(cancel_requested=True)
        messages.success(request, "Cancelling — the worker will stop after the current frame.")
        audit(request, "run.cancel", target=run)
    return redirect("monitoring:run_detail", pk=run.pk)


@login_required
@manager_required
@require_POST
def run_retry(request, pk: int):
    run = get_object_or_404(DetectionRun, pk=pk)
    try:
        new_run = retry_run(run, request.user)
    except ServiceError as exc:
        messages.error(request, str(exc))
        return redirect("monitoring:run_detail", pk=run.pk)
    audit(request, "run.retry", target=new_run, detail=f"from={run.pk}")
    messages.success(request, "New run queued.")
    return redirect("monitoring:run_detail", pk=new_run.pk)


@login_required
@admin_required
@require_POST
def run_delete(request, pk: int):
    run = get_object_or_404(DetectionRun, pk=pk)
    if not run.is_finished:
        messages.error(request, "Stop the run before deleting it.")
        return redirect("monitoring:run_detail", pk=run.pk)
    for field in ("output_video", "thumbnail"):
        file_field = getattr(run, field)
        if file_field:
            Path(file_field.path).unlink(missing_ok=True)
    audit(request, "run.delete", target=run, detail=run.title)
    run.delete()
    messages.success(request, "Run deleted.")
    return redirect("monitoring:runs")


@login_required
@require_GET
def run_video(request, pk: int):
    run = get_object_or_404(DetectionRun, pk=pk)
    if not run.output_video:
        raise Http404("This run has no annotated video.")
    path = Path(run.output_video.path)
    if not path.exists():
        raise Http404("The annotated video is missing from storage.")
    return range_file_response(request, path, "video/mp4", f"{run.pk}.mp4")


@login_required
@require_GET
def run_thumbnail(request, pk: int):
    run = get_object_or_404(DetectionRun, pk=pk)
    if not run.thumbnail:
        raise Http404("No thumbnail for this run.")
    path = Path(run.thumbnail.path)
    if not path.exists():
        raise Http404("Thumbnail missing.")
    response = FileResponse(open(path, "rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=600"
    return response


@login_required
@require_GET
def run_export(request, pk: int):
    run = get_object_or_404(DetectionRun, pk=pk)
    dataset = request.GET.get("dataset", "tracks")

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="run-{run.pk}-{dataset}.csv"'
    writer = csv.writer(response)

    if dataset == "samples":
        writer.writerow(["timestamp_seconds", "occupancy", "unique_total", "line_in", "line_out"])
        for sample in run.samples.order_by("timestamp_seconds").iterator():
            writer.writerow(
                [
                    f"{sample.timestamp_seconds:.3f}",
                    sample.occupancy,
                    sample.unique_total,
                    sample.line_in,
                    sample.line_out,
                ]
            )
    else:
        writer.writerow(
            [
                "track_id",
                "first_seen_seconds",
                "last_seen_seconds",
                "visible_seconds",
                "frames_seen",
                "confidence",
                "counted",
                "last_bbox",
            ]
        )
        for track in run.tracks.order_by("track_id").iterator():
            writer.writerow(
                [
                    track.track_id,
                    f"{track.first_seen_seconds:.3f}",
                    f"{track.last_seen_seconds:.3f}",
                    f"{track.visible_seconds:.3f}",
                    track.frames_seen,
                    f"{track.confidence:.2f}",
                    int(track.is_counted),
                    json.dumps(track.last_bbox or []),
                ]
            )
    audit(request, "run.export", target=run, detail=dataset)
    return response


# ---------------------------------------------------------------------------
# Live streaming / polling APIs
# ---------------------------------------------------------------------------


@login_required
@require_GET
def live_frame(request, pk: int):
    payload = read_live_frame(pk)
    if payload is None:
        raise Http404("No live frame available.")
    response = HttpResponse(payload, content_type="image/jpeg")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@login_required
@require_GET
def live_stream(request, pk: int):
    camera = get_object_or_404(Camera, pk=pk)
    response = StreamingHttpResponse(
        mjpeg_stream(camera.pk),
        content_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["X-Accel-Buffering"] = "no"
    response["Pragma"] = "no-cache"
    return response


def _run_payload(run: DetectionRun) -> dict:
    sample = latest_live_sample(run)
    return {
        "ok": True,
        "run_id": run.pk,
        "status": run.status,
        "status_display": run.get_status_display(),
        "progress": run.progress,
        "frames_processed": run.frames_processed,
        "frames_total": run.frames_total,
        "processing_fps": run.processing_fps,
        "unique_birds": run.unique_birds,
        "peak_occupancy": run.peak_occupancy,
        "average_occupancy": run.average_occupancy,
        "line_in": run.line_in,
        "line_out": run.line_out,
        "occupancy": sample.occupancy if sample else 0,
        "sample_timestamp": sample.timestamp_seconds if sample else None,
        "camera_status": run.camera.status if run.camera else None,
        "finished": run.is_finished,
        "error": run.error_message,
        "elapsed": run.elapsed_display,
    }


@login_required
@require_GET
def api_run_progress(request, pk: int):
    run = get_object_or_404(DetectionRun.objects.select_related("camera"), pk=pk)
    return JsonResponse(_run_payload(run))


@login_required
@require_GET
def api_camera_live(request, pk: int):
    camera = get_object_or_404(Camera, pk=pk)
    run = active_run_for_camera(camera)
    payload = {"ok": True, "camera": {"id": camera.pk, "status": camera.status}}
    if run is None:
        payload["run"] = None
        payload["frame_age"] = live_frame_age(camera.pk)
        return JsonResponse(payload)
    payload["run"] = _run_payload(run)
    payload["frame_age"] = live_frame_age(camera.pk)
    return JsonResponse(payload)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@login_required
@require_GET
def alert_list(request):
    alerts = Alert.objects.select_related("camera", "run", "acknowledged_by")
    show = request.GET.get("show", "open")
    if show == "open":
        alerts = alerts.filter(is_acknowledged=False)
    elif show == "acknowledged":
        alerts = alerts.filter(is_acknowledged=True)
    page = _page(request, alerts, per_page=30)
    return render(
        request,
        "monitoring/alerts.html",
        {"page": page, "show": show, "can_manage": request.user.is_manager},
    )


@login_required
@manager_required
@require_POST
def alert_acknowledge(request, pk: int):
    alert = get_object_or_404(Alert, pk=pk)
    acknowledge_alert(alert, request.user)
    audit(request, "alert.acknowledge", target=alert, detail=alert.kind)
    messages.success(request, "Alert acknowledged.")
    return redirect(request.POST.get("next") or "monitoring:alerts")


# ---------------------------------------------------------------------------
# Reports and audit
# ---------------------------------------------------------------------------


def _report_rows(days: int):
    start = timezone.now() - timedelta(days=days)
    return (
        DetectionRun.objects.filter(created_at__gte=start)
        .annotate(day=TruncDate("created_at"))
        .values("day", "camera__name", "source_type")
        .annotate(
            runs=Count("id"),
            unique=Sum("unique_birds"),
            peak=Max("peak_occupancy"),
            average=Avg("average_occupancy"),
            line_in=Sum("line_in"),
            line_out=Sum("line_out"),
        )
        .order_by("-day", "camera__name")
    )


def _clamp_days(raw: str | None) -> int:
    try:
        days = int(raw or 7)
    except (TypeError, ValueError):
        days = 7
    return max(1, min(days, 90))


@login_required
@require_GET
def reports(request):
    days = _clamp_days(request.GET.get("days"))
    rows = list(_report_rows(days))
    totals = {
        "runs": sum(row["runs"] for row in rows),
        "unique": sum(row["unique"] or 0 for row in rows),
        "line_in": sum(row["line_in"] or 0 for row in rows),
        "line_out": sum(row["line_out"] or 0 for row in rows),
    }
    totals["net"] = totals["line_in"] - totals["line_out"]
    return render(
        request,
        "monitoring/reports.html",
        {"rows": rows, "days": days, "totals": totals},
    )


@login_required
@require_GET
def reports_export(request):
    days = _clamp_days(request.GET.get("days"))
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="chicken-report-{days}d.csv"'
    writer = csv.writer(response)
    writer.writerow(
        ["date", "camera", "source", "runs", "unique_birds", "peak_occupancy", "avg_occupancy", "line_in", "line_out"]
    )
    for row in _report_rows(days):
        writer.writerow(
            [
                row["day"],
                row["camera__name"] or "—",
                row["source_type"],
                row["runs"],
                row["unique"] or 0,
                row["peak"] or 0,
                round(row["average"] or 0, 2),
                row["line_in"] or 0,
                row["line_out"] or 0,
            ]
        )
    audit(request, "reports.export", detail=f"days={days}")
    return response


@login_required
@admin_required
@require_GET
def audit_log(request):
    entries = AuditLog.objects.select_related("user")
    action = request.GET.get("action", "")
    if action:
        entries = entries.filter(action=action)
    page = _page(request, entries, per_page=50)
    actions = (
        AuditLog.objects.values_list("action", flat=True).distinct().order_by("action")[:60]
    )
    return render(
        request,
        "monitoring/audit.html",
        {"page": page, "actions": actions, "action": action},
    )


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


@login_not_required
@require_GET
def healthz(request):
    """Unauthenticated liveness probe with no sensitive detail."""
    database_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # pragma: no cover
        database_ok = False
    payload = {"status": "ok" if database_ok else "degraded", "database": database_ok}
    return JsonResponse(payload, status=200 if database_ok else 503)


@login_not_required
def csrf_failure(request, reason: str = ""):
    if request.path.startswith("/api/") or "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"ok": False, "error": "CSRF verification failed."}, status=403)
    return render(request, "monitoring/403.html", {"reason": reason}, status=403)


@login_required
@require_GET
def run_chart_data(request, pk: int):
    run = get_object_or_404(DetectionRun, pk=pk)
    samples = list(
        run.samples.order_by("timestamp_seconds").values(
            "timestamp_seconds", "occupancy", "unique_total", "line_in", "line_out"
        )
    )
    return JsonResponse({"ok": True, "points": downsample(samples, 240)})


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@login_not_required
def page_not_found(request, exception=None):
    return render(request, "monitoring/404.html", {}, status=404)


@login_not_required
def server_error(request):
    return render(request, "monitoring/500.html", {}, status=500)


@login_not_required
def permission_denied(request, exception=None):
    reason = ""
    if exception is not None:
        reason = str(exception).strip()
    return render(request, "monitoring/403.html", {"reason": reason}, status=403)
