"""Execution of detection runs: uploaded videos and live camera sessions.

A run is a single ``DetectionRun`` row. The pipeline streams frames (never loading
a whole video in memory), detects, tracks, counts, annotates, and writes periodic
samples so the web UI can follow progress without touching the worker process.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone

from monitoring.models import (
    Alert,
    BirdTrack,
    Camera,
    CountSample,
    DetectionRun,
    Zone,
    ZoneStatistic,
)

from .annotate import annotate_frame
from .counting import CountingEngine, Snapshot, ZoneSpec
from .detector import DetectorError, get_detector
from .geometry import bbox_foot, point_in_polygon, to_pixels
from .media import (
    MediaError,
    open_capture,
    probe_video,
    transcode_h264,
    write_jpeg_atomic,
    write_thumbnail,
)
from .tracker import build_tracker

logger = logging.getLogger("monitoring.ml.pipeline")


class PipelineError(RuntimeError):
    """A run cannot be executed (bad source, unreadable stream, missing model)."""


class RunCancelled(Exception):
    """Raised internally when a user cancels a run."""


@dataclass
class PipelineConfig:
    model_path: str
    device: str = "cpu"
    class_ids: str = ""
    torch_threads: int = 2
    confidence: float = 0.35
    iou: float = 0.50
    imgsz: int = 640
    frame_skip: int = 2
    min_track_hits: int = 5
    max_age: int = 45
    n_init: int = 3
    dedupe_window_s: float = 6.0
    sample_interval_s: float = 1.0
    live_fps: int = 10
    roi: list | None = None
    roi_only: bool = False
    counting_line: list | None = None
    invert_line: bool = False
    zones: list[dict] = field(default_factory=list)

    @classmethod
    def from_run(cls, run: DetectionRun) -> "PipelineConfig":
        camera = run.camera
        config = cls(
            model_path=getattr(settings, "CHICKEN_MODEL_PATH", "yolo11n.pt"),
            device=getattr(settings, "CHICKEN_DEVICE", "cpu"),
            class_ids=getattr(settings, "CHICKEN_CLASS_IDS", ""),
            torch_threads=getattr(settings, "CHICKEN_TORCH_THREADS", 2),
            confidence=float(camera.confidence) if camera else getattr(settings, "CHICKEN_CONFIDENCE", 0.35),
            iou=float(camera.iou) if camera else getattr(settings, "CHICKEN_IOU", 0.5),
            imgsz=int(camera.imgsz) if camera else getattr(settings, "CHICKEN_IMGSZ", 640),
            frame_skip=max(1, int(camera.frame_skip) if camera else 2),
            min_track_hits=int(camera.min_track_hits) if camera else getattr(settings, "CHICKEN_MIN_TRACK_HITS", 5),
            max_age=getattr(settings, "CHICKEN_MAX_AGE", 45),
            n_init=getattr(settings, "CHICKEN_N_INIT", 3),
            dedupe_window_s=getattr(settings, "CHICKEN_DEDUPE_WINDOW_S", 6.0),
            sample_interval_s=getattr(settings, "CHICKEN_SAMPLE_INTERVAL_S", 1.0),
            live_fps=getattr(settings, "CHICKEN_LIVE_FPS", 10),
            roi=list(camera.roi) if camera and camera.roi else None,
            roi_only=bool(camera.roi_only) if camera else False,
            counting_line=list(camera.counting_line) if camera and camera.counting_line else None,
            invert_line=bool(camera.invert_line) if camera else False,
            zones=list(
                Zone.objects.filter(camera=camera, is_active=True).values(
                    "name", "kind", "polygon"
                )
            )
            if camera
            else [],
        )
        # Video runs may borrow a camera profile (ROI, counting line, tuning).
        profile = (run.config or {}).get("profile") if isinstance(run.config, dict) else None
        if isinstance(profile, dict):
            for key in (
                "confidence",
                "iou",
                "imgsz",
                "frame_skip",
                "min_track_hits",
                "roi",
                "roi_only",
                "counting_line",
                "invert_line",
                "dedupe_window_s",
                "sample_interval_s",
                "zones",
            ):
                if profile.get(key) is not None:
                    setattr(config, key, profile[key])
        return config

    def profile(self) -> dict:
        """Serialisable settings copied from a camera profile."""
        return {
            "confidence": self.confidence,
            "iou": self.iou,
            "imgsz": self.imgsz,
            "frame_skip": self.frame_skip,
            "min_track_hits": self.min_track_hits,
            "roi": self.roi,
            "roi_only": self.roi_only,
            "counting_line": self.counting_line,
            "invert_line": self.invert_line,
            "dedupe_window_s": self.dedupe_window_s,
            "sample_interval_s": self.sample_interval_s,
            "zones": self.zones,
        }


# ---------------------------------------------------------------------------
# Database writer
# ---------------------------------------------------------------------------


class RunWriter:
    """Batches database writes so inference stays the bottleneck, not SQL."""

    def __init__(self, run_id: int, sample_interval_s: float = 1.0) -> None:
        self.run_id = run_id
        self.sample_interval_s = max(0.05, sample_interval_s)
        self._sample_buffer: list[CountSample] = []
        self._last_sample_ts = float("-inf")
        self._last_update = 0.0
        self._last_cancel_check = 0.0
        self._cancel_requested = False
        self.samples_written = 0

    def maybe_sample(self, snapshot: Snapshot) -> None:
        if snapshot.timestamp_s - self._last_sample_ts < self.sample_interval_s:
            return
        if snapshot.timestamp_s <= self._last_sample_ts:
            return
        self._last_sample_ts = snapshot.timestamp_s
        self._sample_buffer.append(
            CountSample(
                run_id=self.run_id,
                frame_index=snapshot.frame_index,
                timestamp_seconds=round(snapshot.timestamp_s, 3),
                occupancy=snapshot.occupancy,
                unique_total=snapshot.unique_total,
                line_in=snapshot.line_in,
                line_out=snapshot.line_out,
            )
        )
        if len(self._sample_buffer) >= 30:
            self.flush_samples()

    def flush_samples(self) -> None:
        if not self._sample_buffer:
            return
        batch = self._sample_buffer
        self._sample_buffer = []
        try:
            CountSample.objects.bulk_create(batch, batch_size=500)
            self.samples_written += len(batch)
        except Exception:  # pragma: no cover - never lose the run over a sample batch
            logger.exception("Could not persist %s samples for run %s", len(batch), self.run_id)

    def update_progress(
        self,
        snapshot: Snapshot,
        *,
        frames_processed: int,
        frames_total: int,
        fps: float,
        progress: int | None = None,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last_update < 1.5:
            return
        self._last_update = now
        fields = {
            "frames_processed": frames_processed,
            "frames_total": frames_total,
            "processing_fps": round(float(fps or 0), 2),
            "unique_birds": snapshot.unique_total,
            "line_in": snapshot.line_in,
            "line_out": snapshot.line_out,
            "sample_count": self.samples_written + len(self._sample_buffer),
        }
        if progress is not None:
            fields["progress"] = max(0, min(99, int(progress)))
        # `peak_occupancy` is merged with a Greatest() so throttled updates never lose a peak.
        try:
            DetectionRun.objects.filter(pk=self.run_id).update(**fields)
            DetectionRun.objects.filter(
                pk=self.run_id, peak_occupancy__lt=snapshot.occupancy
            ).update(peak_occupancy=snapshot.occupancy)
        except Exception:  # pragma: no cover
            logger.exception("Could not update progress for run %s", self.run_id)

    def cancel_requested(self) -> bool:
        now = time.monotonic()
        if now - self._last_cancel_check < 0.75:
            return self._cancel_requested
        self._last_cancel_check = now
        try:
            self._cancel_requested = bool(
                DetectionRun.objects.filter(pk=self.run_id).values_list(
                    "cancel_requested", flat=True
                ).first()
            )
        except Exception:  # pragma: no cover
            logger.exception("Could not poll cancel flag for run %s", self.run_id)
        return self._cancel_requested


# ---------------------------------------------------------------------------
# Frame processor
# ---------------------------------------------------------------------------


class FrameProcessor:
    """Detector + tracker + counter for one run, plus annotation."""

    def __init__(self, run: DetectionRun, config: PipelineConfig, source_label: str = "") -> None:
        self.run = run
        self.config = config
        self.source_label = source_label
        try:
            self.detector = get_detector(
                config.model_path,
                device=config.device,
                class_ids=config.class_ids,
                torch_threads=config.torch_threads,
            )
        except DetectorError as exc:
            raise PipelineError(str(exc)) from exc

        self.tracker = build_tracker(max_age=config.max_age, n_init=config.n_init)
        if self.tracker.degraded:
            logger.warning("Run %s is using the IoU fallback tracker.", run.pk)

        self._roi_px: list | None = None
        self._line_px: list | None = None
        self._zone_specs: list[ZoneSpec] = []
        self._geometry_ready = False
        self.counter: CountingEngine | None = None
        self.fps = 0.0
        self._last_tick = time.perf_counter()
        self.frames_processed = 0
        self._best_thumbnail = None
        self._thumbnail_score = -1.0

    @property
    def backend(self) -> str:
        return self.tracker.backend

    def _prepare_geometry(self, width: int, height: int) -> None:
        if self._geometry_ready:
            return
        if self.config.roi:
            self._roi_px = to_pixels(self.config.roi, width, height)
        if self.config.counting_line:
            self._line_px = to_pixels(self.config.counting_line, width, height)
        self._zone_specs = [
            ZoneSpec(name=zone["name"], kind=zone.get("kind", ""), polygon=to_pixels(zone["polygon"], width, height))
            for zone in self.config.zones
            if zone.get("polygon")
        ]
        self.counter = CountingEngine(
            min_hits=self.config.min_track_hits,
            line=self._line_px,
            invert_line=self.config.invert_line,
            zones=self._zone_specs,
            max_age_frames=self.config.max_age,
            dedupe_window_s=self.config.dedupe_window_s,
        )
        self._geometry_ready = True

    def process(self, frame, frame_index: int, timestamp_s: float) -> tuple[Snapshot, "object"]:
        height, width = frame.shape[:2]
        self._prepare_geometry(width, height)
        assert self.counter is not None

        started = time.perf_counter()
        detections = self.detector.detect(
            frame,
            confidence=self.config.confidence,
            iou=self.config.iou,
            imgsz=self.config.imgsz,
        )
        if self._roi_px and self.config.roi_only:
            detections = [
                detection
                for detection in detections
                if point_in_polygon(bbox_foot(detection.bbox), self._roi_px)
            ]
        tracks = self.tracker.update(detections, frame)
        snapshot = self.counter.update(frame_index, timestamp_s, tracks, width, height)

        elapsed = max(1e-6, time.perf_counter() - started)
        instant_fps = 1.0 / elapsed
        self.fps = instant_fps if self.fps == 0 else 0.85 * self.fps + 0.15 * instant_fps
        self.frames_processed += 1

        annotated = annotate_frame(
            frame,
            snapshot,
            roi=self.config.roi,
            line=self.config.counting_line,
            invert_line=self.config.invert_line,
            fps=self.fps,
            source_label=self.source_label,
        )
        self._remember_thumbnail(annotated, snapshot)
        return snapshot, annotated

    def _remember_thumbnail(self, annotated, snapshot: Snapshot) -> None:
        """Keep a representative annotated frame (prefer one with birds)."""
        score = float(snapshot.occupancy) + len(snapshot.tracks) * 0.1
        if snapshot.frame_index % 5 == 0 and score >= self._thumbnail_score:
            self._thumbnail_score = score
            self._best_thumbnail = annotated.copy()

    @property
    def thumbnail(self):
        return self._best_thumbnail

    def track_records(self) -> list[dict]:
        return self.counter.track_records() if self.counter else []

    def summary(self) -> dict:
        return self.counter.summary() if self.counter else {}

    def zone_summary(self) -> list[dict]:
        return self.counter.zone_summary() if self.counter else []


# ---------------------------------------------------------------------------
# Finalisation
# ---------------------------------------------------------------------------


def _finalise(
    run: DetectionRun,
    processor: FrameProcessor | None,
    writer: RunWriter,
    *,
    status: str,
    raw_output: Path | None = None,
    error_message: str = "",
    frames_processed: int = 0,
    frames_total: int = 0,
) -> None:
    writer.flush_samples()
    summary = processor.summary() if processor else {}
    now = timezone.now()

    if processor is not None:
        _persist_tracks(run, processor.track_records())
        _persist_zone_stats(run, processor.zone_summary())

    output_name = ""
    if raw_output is not None and raw_output.exists() and raw_output.stat().st_size > 0:
        final_path = Path(settings.OUTPUT_ROOT) / f"{run.pk}.mp4"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if not transcode_h264(raw_output, final_path):
            os.replace(raw_output, final_path)
        else:
            raw_output.unlink(missing_ok=True)
        output_name = str(final_path.relative_to(settings.MEDIA_ROOT))

    thumbnail_name = ""
    if processor is not None and processor.thumbnail is not None:
        thumb_path = Path(settings.THUMBNAIL_ROOT) / f"{run.pk}.jpg"
        if write_thumbnail(processor.thumbnail, thumb_path):
            thumbnail_name = str(thumb_path.relative_to(settings.MEDIA_ROOT))

    elapsed = None
    if run.started_at:
        elapsed = max(0.0, (now - run.started_at).total_seconds())

    DetectionRun.objects.filter(pk=run.pk).update(
        status=status,
        finished_at=now,
        progress=100 if status == DetectionRun.Status.COMPLETED else run.progress,
        frames_processed=frames_processed,
        frames_total=frames_total or run.frames_total,
        processing_fps=round(processor.fps if processor else 0.0, 2),
        unique_birds=summary.get("unique_birds", 0),
        peak_occupancy=summary.get("peak_occupancy", 0),
        average_occupancy=summary.get("average_occupancy", 0.0),
        line_in=summary.get("line_in", 0),
        line_out=summary.get("line_out", 0),
        sample_count=writer.samples_written,
        error_message=error_message[:2000],
        output_video=output_name,
        thumbnail=thumbnail_name,
    )

    camera = run.camera
    if camera is not None:
        Camera.objects.filter(pk=camera.pk).update(
            status=Camera.Status.OFFLINE,
            last_seen_at=now,
            last_error=error_message[:300],
        )
        _clear_live_frame(camera.pk)

    logger.info(
        "Run %s finished status=%s unique=%s peak=%s frames=%s elapsed=%ss",
        run.pk,
        status,
        summary.get("unique_birds"),
        summary.get("peak_occupancy"),
        frames_processed,
        round(elapsed, 1) if elapsed else "?",
    )


def _persist_tracks(run: DetectionRun, records: list[dict]) -> None:
    if not records:
        return
    BirdTrack.objects.filter(run=run).delete()
    batch = [
        BirdTrack(
            run=run,
            track_id=record["root_id"],
            first_frame=record["first_frame"],
            last_frame=record["last_frame"],
            first_seen_seconds=round(record["first_seen_s"], 3),
            last_seen_seconds=round(record["last_seen_s"], 3),
            frames_seen=record["frames_seen"],
            confidence=round(record["confidence"], 4),
            is_counted=record["is_counted"],
            last_bbox=record["last_bbox"],
        )
        for record in records
    ]
    BirdTrack.objects.bulk_create(batch, batch_size=500)


def _persist_zone_stats(run: DetectionRun, zone_summary: list[dict]) -> None:
    if not zone_summary:
        return
    zones = {zone.name: zone for zone in Zone.objects.filter(camera=run.camera)}
    ZoneStatistic.objects.filter(run=run).delete()
    ZoneStatistic.objects.bulk_create(
        [
            ZoneStatistic(
                run=run,
                zone=zones.get(item["name"]),
                zone_name=item["name"],
                zone_kind=item.get("kind", ""),
                peak_occupancy=item["peak"],
                average_occupancy=round(item["average"], 2),
            )
            for item in zone_summary
        ]
    )


def _merge_run_config(run_id: int, patch: dict) -> None:
    current = DetectionRun.objects.filter(pk=run_id).values_list("config", flat=True).first() or {}
    merged = {**current, **patch}
    DetectionRun.objects.filter(pk=run_id).update(config=merged)


def _clear_live_frame(camera_id: int) -> None:
    path = Path(settings.LIVE_FRAME_ROOT) / f"{camera_id}.jpg"
    path.unlink(missing_ok=True)


def _maybe_overcrowding_alert(run: DetectionRun, snapshot: Snapshot, raised: set[str]) -> None:
    camera = run.camera
    threshold = getattr(camera, "occupancy_alert_threshold", None) if camera else None
    if not threshold or snapshot.occupancy < threshold or "overcrowding" in raised:
        return
    raised.add("overcrowding")
    Alert.objects.create(
        camera=camera,
        run=run,
        kind=Alert.Kind.OVERCROWDING,
        severity=Alert.Severity.WARNING,
        message=f"{snapshot.occupancy} birds visible (threshold {threshold})",
        metadata={"occupancy": snapshot.occupancy, "threshold": threshold},
    )
    logger.info("Overcrowding alert raised for run %s", run.pk)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def execute_run(run_id: int) -> None:
    """Execute one queued run. Never raises: failures are stored on the run."""
    close_old_connections()
    run = None
    try:
        run = DetectionRun.objects.select_related("video", "camera").get(pk=run_id)
        DetectionRun.objects.filter(pk=run_id).update(
            status=DetectionRun.Status.RUNNING,
            started_at=timezone.now(),
            progress=0,
            error_message="",
            cancel_requested=False,
        )
        if run.source_type == DetectionRun.SourceType.CAMERA:
            _execute_camera(run)
        else:
            _execute_video(run)
    except RunCancelled:
        logger.info("Run %s cancelled", run_id)
        if run is not None:
            _finalise(run, None, RunWriter(run_id), status=DetectionRun.Status.CANCELLED)
    except (PipelineError, MediaError, DetectorError) as exc:
        logger.warning("Run %s failed: %s", run_id, exc)
        _mark_failed(run, str(exc))
    except Exception as exc:  # noqa: BLE001 - a bad video must never kill the worker
        logger.exception("Unexpected failure in run %s", run_id)
        _mark_failed(run, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            connections.close_all()
        except Exception:  # pragma: no cover
            pass


def _mark_failed(run: DetectionRun | None, message: str) -> None:
    if run is None:
        return
    DetectionRun.objects.filter(pk=run.pk).update(
        status=DetectionRun.Status.FAILED,
        finished_at=timezone.now(),
        error_message=message[:2000],
    )
    if run.camera_id:
        Camera.objects.filter(pk=run.camera_id).update(
            status=Camera.Status.ERROR, last_error=message[:300]
        )
        _clear_live_frame(run.camera_id)
    try:
        Alert.objects.create(
            camera=run.camera,
            run=run,
            kind=Alert.Kind.RUN_FAILED,
            severity=Alert.Severity.CRITICAL,
            message=message[:300],
        )
    except Exception:  # pragma: no cover
        logger.exception("Could not create failure alert for run %s", run.pk)


# ---------------------------------------------------------------------------
# Video runs
# ---------------------------------------------------------------------------


def _execute_video(run: DetectionRun) -> None:
    video = run.video
    if video is None:
        raise PipelineError("This run has no video attached.")
    path = Path(video.file.path)
    if not path.exists():
        raise PipelineError("The source video is missing from storage.")

    config = PipelineConfig.from_run(run)
    probe = probe_video(path)
    if probe.duration_seconds > settings.CHICKEN_MAX_VIDEO_SECONDS:
        raise PipelineError(
            f"Video is longer than the {settings.CHICKEN_MAX_VIDEO_SECONDS // 60} minute limit."
        )

    fps = max(1.0, probe.fps)
    frame_skip = config.frame_skip
    output_fps = max(1.0, fps / frame_skip)
    total_frames = probe.frame_count or int(probe.duration_seconds * fps)

    DetectionRun.objects.filter(pk=run.pk).update(
        frames_total=total_frames,
        detector_model=Path(config.model_path).name,
        device=config.device,
    )
    _merge_run_config(
        run.pk,
        {
            "runtime": {
                "confidence": config.confidence,
                "iou": config.iou,
                "imgsz": config.imgsz,
                "frame_skip": frame_skip,
                "min_track_hits": config.min_track_hits,
                "fps": fps,
                "resolution": probe.resolution,
            }
        },
    )

    processor = FrameProcessor(run, config, source_label=run.camera.name if run.camera else "video")
    # Report the weights that actually loaded (a missing custom checkpoint falls back).
    DetectionRun.objects.filter(pk=run.pk).update(
        detector_model=processor.detector.describe, device=config.device
    )
    writer = RunWriter(run.pk, sample_interval_s=config.sample_interval_s)

    raw_path = Path(settings.OUTPUT_ROOT) / f"{run.pk}-raw.mp4"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    output = cv2.VideoWriter(str(raw_path), fourcc, output_fps, (probe.width, probe.height))
    if not output.isOpened():
        raise PipelineError("Cannot create the annotated output video on disk.")

    capture = open_capture(str(path))
    if not capture.isOpened():
        output.release()
        raise PipelineError("The video could not be opened for decoding.")

    frames_processed = 0
    frame_index = 0
    status = DetectionRun.Status.COMPLETED
    error_message = ""
    raised_alerts: set[str] = set()
    try:
        while True:
            if writer.cancel_requested():
                status = DetectionRun.Status.CANCELLED
                break
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            if frame.shape[1] != probe.width or frame.shape[0] != probe.height:
                frame = cv2.resize(frame, (probe.width, probe.height))

            if frame_index % frame_skip == 0:
                timestamp_s = frame_index / fps
                snapshot, annotated = processor.process(frame, frame_index, timestamp_s)
                output.write(annotated)
                writer.maybe_sample(snapshot)
                frames_processed += 1
                _maybe_overcrowding_alert(run, snapshot, raised_alerts)

                progress = int(100 * frame_index / total_frames) if total_frames else 0
                writer.update_progress(
                    snapshot,
                    frames_processed=frames_processed,
                    frames_total=total_frames,
                    fps=processor.fps,
                    progress=progress,
                )
            frame_index += 1
            if total_frames and frame_index > total_frames + 5:
                break
    finally:
        capture.release()
        output.release()

    _finalise(
        run,
        processor,
        writer,
        status=status,
        raw_output=raw_path,
        error_message=error_message,
        frames_processed=frames_processed,
        frames_total=total_frames,
    )
    if status == DetectionRun.Status.CANCELLED:
        logger.info("Video run %s cancelled after %s frames", run.pk, frames_processed)


# ---------------------------------------------------------------------------
# Live camera runs
# ---------------------------------------------------------------------------


def _set_camera_status(camera_id: int, status: str, error: str = "") -> None:
    Camera.objects.filter(pk=camera_id).update(
        status=status, last_error=error[:300], last_seen_at=timezone.now()
    )


def _execute_camera(run: DetectionRun) -> None:
    camera = run.camera
    if camera is None:
        raise PipelineError("This run has no camera attached.")
    if not camera.stream_url:
        raise PipelineError(
            "This camera has no stream URL. Add an RTSP/HTTP URL before starting it."
        )

    config = PipelineConfig.from_run(run)
    config.sample_interval_s = max(2.0, config.sample_interval_s)
    processor = FrameProcessor(run, config, source_label=camera.name)
    writer = RunWriter(run.pk, sample_interval_s=config.sample_interval_s)
    live_path = Path(settings.LIVE_FRAME_ROOT) / f"{camera.pk}.jpg"
    frame_skip = config.frame_skip

    DetectionRun.objects.filter(pk=run.pk).update(
        frames_total=0,
        detector_model=processor.detector.describe,
        device=config.device,
    )
    _merge_run_config(
        run.pk,
        {
            "runtime": {
                "confidence": config.confidence,
                "iou": config.iou,
                "imgsz": config.imgsz,
                "frame_skip": frame_skip,
                "min_track_hits": config.min_track_hits,
                "live": True,
            }
        },
    )

    capture = None
    status = DetectionRun.Status.CANCELLED
    error_message = ""
    raised_alerts: set[str] = set()
    processed = 0
    frame_index = 0
    started = time.monotonic()
    failure_streak = 0
    last_publish = 0.0
    publish_interval = 1.0 / max(1, config.live_fps)

    _set_camera_status(camera.pk, Camera.Status.STARTING)
    try:
        while True:
            if writer.cancel_requested():
                status = DetectionRun.Status.CANCELLED
                break

            if capture is None:
                capture = open_capture(camera.stream_url)
                if not capture.isOpened():
                    capture.release()
                    capture = None
                    failure_streak += 1
                    _set_camera_status(
                        camera.pk, Camera.Status.ERROR, "Cannot open the camera stream."
                    )
                    if failure_streak == 3 and "camera_offline" not in raised_alerts:
                        raised_alerts.add("camera_offline")
                        Alert.objects.create(
                            camera=camera,
                            run=run,
                            kind=Alert.Kind.CAMERA_OFFLINE,
                            severity=Alert.Severity.CRITICAL,
                            message=f"Cannot open stream for camera {camera.name}",
                        )
                    # Back off (1s → 30s) but stay responsive to cancellation.
                    delay = min(30.0, 2.0 ** min(failure_streak, 5))
                    slept = 0.0
                    while slept < delay and not writer.cancel_requested():
                        time.sleep(0.5)
                        slept += 0.5
                    continue

                failure_streak = 0
                Camera.objects.filter(pk=camera.pk).update(
                    status=Camera.Status.ONLINE,
                    last_seen_at=timezone.now(),
                    last_error="",
                )
                logger.info("Camera %s connected for run %s", camera.pk, run.pk)

            ok, frame = capture.read()
            if not ok or frame is None:
                capture.release()
                capture = None
                _set_camera_status(camera.pk, Camera.Status.ERROR, "Stream interrupted; reconnecting.")
                time.sleep(min(2.0, 0.5 + failure_streak))
                failure_streak += 1
                continue

            frame_index += 1
            if frame_index % frame_skip:
                continue

            timestamp_s = time.monotonic() - started
            snapshot, annotated = processor.process(frame, frame_index, timestamp_s)
            processed += 1

            now = time.monotonic()
            if now - last_publish >= publish_interval:
                write_jpeg_atomic(live_path, annotated, quality=72)
                last_publish = now

            writer.maybe_sample(snapshot)
            writer.update_progress(
                snapshot,
                frames_processed=processed,
                frames_total=0,
                fps=processor.fps,
                progress=0,
            )
            _maybe_overcrowding_alert(run, snapshot, raised_alerts)

    finally:
        if capture is not None:
            capture.release()
        _finalise(
            run,
            processor,
            writer,
            status=status,
            error_message=error_message,
            frames_processed=processed,
            frames_total=0,
        )
        _set_camera_status(camera.pk, Camera.Status.OFFLINE)
