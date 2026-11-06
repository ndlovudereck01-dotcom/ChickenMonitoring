"""End-to-end pipeline test with a stubbed detector and tracker.

The real YOLO/DeepSORT stack needs model weights and a GPU-less several seconds per
frame, so these tests replace both with deterministic fakes and check everything
around them: decoding, sampling, annotation, persistence and failure handling.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from accounts.models import User
from monitoring.ml import pipeline as pipeline_module
from monitoring.ml.detector import Detection, DetectorError
from monitoring.ml.tracker import ActiveTrack
from monitoring.models import BirdTrack, Camera, CountSample, DetectionRun, VideoAsset


class MovingDetector:
    """Reports one bird that drifts across the frame."""

    describe = "stub-detector"

    def __init__(self, *_args, **_kwargs):
        self.step = 0

    def detect(self, frame, **_kwargs):
        height, width = frame.shape[:2]
        x = 10 + (self.step * 6) % max(1, width - 40)
        y = int(height * 0.45)
        self.step += 1
        return [Detection(bbox=(x, y, x + 22, y + 24), confidence=0.92, class_id=0, label="bird")]


class FixedTracker:
    degraded = False
    backend = "stub"

    def __init__(self, *_args, **_kwargs):
        pass

    def update(self, detections, frame):  # noqa: ARG002
        if not detections:
            return []
        detection = detections[0]
        return [
            ActiveTrack(
                track_id=1,
                bbox=detection.bbox,
                confidence=detection.confidence,
                hits=8,
                age=8,
            )
        ]


def write_test_video(path: Path, frames: int = 12, size=(160, 120), fps: int = 10) -> None:
    width, height = size
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError("OpenCV cannot write mp4 on this machine")
    try:
        for index in range(frames):
            frame = np.full((height, width, 3), 30, dtype=np.uint8)
            x = 10 + (index * 6) % max(1, width - 40)
            cv2.rectangle(frame, (x, 60), (x + 22, 84), (235, 235, 235), -1)
            writer.write(frame)
    finally:
        writer.release()


class PipelineTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chickenvision-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("private", "private/videos", "private/annotated", "private/thumbnails", "private/live"):
            (self.tmp / name).mkdir(parents=True, exist_ok=True)
        self.settings_override = override_settings(
            MEDIA_ROOT=self.tmp,
            PRIVATE_MEDIA_ROOT=self.tmp / "private",
            UPLOAD_ROOT=self.tmp / "private" / "videos",
            OUTPUT_ROOT=self.tmp / "private" / "annotated",
            THUMBNAIL_ROOT=self.tmp / "private" / "thumbnails",
            LIVE_FRAME_ROOT=self.tmp / "private" / "live",
            CHICKEN_MAX_VIDEO_SECONDS=600,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        self.user = User.objects.create_user(username="runner", password="Test-passw0rd-2026")
        self.user.role = User.Role.OPERATOR
        self.user.save(update_fields=["role"])

        self.source = self.tmp / "source.mp4"
        write_test_video(self.source)
        payload = self.source.read_bytes()
        upload = SimpleUploadedFile("house-1.mp4", payload, content_type="video/mp4")
        self.video = VideoAsset.objects.create(
            file=upload,
            original_name="house-1.mp4",
            size_bytes=len(payload),
            duration_seconds=1.2,
            fps=10,
            width=160,
            height=120,
        )

    def _run(self) -> DetectionRun:
        return DetectionRun.objects.create(
            source_type=DetectionRun.SourceType.VIDEO,
            video=self.video,
            title="pipeline test",
            created_by=self.user,
            config={"profile": {"min_track_hits": 1, "frame_skip": 1, "imgsz": 256}},
        )

    def test_run_produces_counts_tracks_and_output(self):
        run = self._run()
        with (
            mock.patch.object(pipeline_module, "get_detector", MovingDetector),
            mock.patch.object(pipeline_module, "build_tracker", FixedTracker),
            mock.patch.object(pipeline_module, "transcode_h264", return_value=False),
        ):
            pipeline_module.execute_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, DetectionRun.Status.COMPLETED, run.error_message)
        self.assertEqual(run.unique_birds, 1)
        self.assertGreaterEqual(run.peak_occupancy, 1)
        self.assertGreaterEqual(run.frames_processed, 8)
        self.assertEqual(run.progress, 100)
        self.assertTrue(run.output_video, "annotated video should be stored")
        self.assertTrue(Path(run.output_video.path).exists())
        self.assertTrue(run.thumbnail)
        self.assertEqual(BirdTrack.objects.filter(run=run).count(), 1)
        self.assertGreaterEqual(CountSample.objects.filter(run=run).count(), 1)

    def test_missing_source_marks_run_failed(self):
        run = self._run()
        self.video.file.delete(save=False)
        with (
            mock.patch.object(pipeline_module, "get_detector", MovingDetector),
            mock.patch.object(pipeline_module, "build_tracker", FixedTracker),
        ):
            pipeline_module.execute_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, DetectionRun.Status.FAILED)
        self.assertIn("missing", run.error_message.lower())
        self.assertIsNotNone(run.finished_at)

    def test_detector_failure_is_reported_not_raised(self):
        run = self._run()

        def broken(*_args, **_kwargs):
            raise DetectorError("weights are corrupt")

        with (
            mock.patch.object(pipeline_module, "get_detector", broken),
            mock.patch.object(pipeline_module, "build_tracker", FixedTracker),
        ):
            pipeline_module.execute_run(run.pk)  # must not raise

        run.refresh_from_db()
        self.assertEqual(run.status, DetectionRun.Status.FAILED)
        self.assertIn("weights are corrupt", run.error_message)

    def test_cancel_request_stops_processing(self):
        run = self._run()
        DetectionRun.objects.filter(pk=run.pk).update(cancel_requested=True)
        with (
            mock.patch.object(pipeline_module, "get_detector", MovingDetector),
            mock.patch.object(pipeline_module, "build_tracker", FixedTracker),
        ):
            pipeline_module.execute_run(run.pk)

        run.refresh_from_db()
        self.assertIn(run.status, {DetectionRun.Status.CANCELLED, DetectionRun.Status.COMPLETED})

    def test_live_camera_run_publishes_frames_until_cancelled(self):
        camera = Camera.objects.create(
            name="Camera source",
            stream_url=str(self.source),
            min_track_hits=1,
            frame_skip=1,
            imgsz=256,
        )
        run = DetectionRun.objects.create(
            source_type=DetectionRun.SourceType.CAMERA,
            camera=camera,
            title="camera test",
            created_by=self.user,
        )
        live_frame = Path(self.tmp) / "private" / "live" / f"{camera.pk}.jpg"
        real_writer = pipeline_module.RunWriter

        class StoppingWriter(real_writer):
            """Stops the live loop after a few iterations, like a user pressing stop."""

            checks = 0

            def cancel_requested(self):
                type(self).checks += 1
                return type(self).checks > 25

        published: list[Path] = []
        real_write = pipeline_module.write_jpeg_atomic

        def write_spy(path, frame, quality=72):
            outcome = real_write(path, frame, quality)
            if outcome:
                published.append(Path(path))
            return outcome

        with (
            mock.patch.object(pipeline_module, "get_detector", MovingDetector),
            mock.patch.object(pipeline_module, "build_tracker", FixedTracker),
            mock.patch.object(pipeline_module, "RunWriter", StoppingWriter),
            mock.patch.object(pipeline_module, "write_jpeg_atomic", write_spy),
        ):
            pipeline_module.execute_run(run.pk)

        self.assertTrue(published, "the live loop should publish annotated JPEG frames")
        self.assertEqual(published[0], live_frame)
        self.assertFalse(
            live_frame.exists(),
            "the stale frame is removed when the session ends so the UI shows offline",
        )
        run.refresh_from_db()
        self.assertEqual(run.status, DetectionRun.Status.CANCELLED)
        self.assertGreaterEqual(run.frames_processed, 1)
        camera.refresh_from_db()
        self.assertIn(camera.status, {Camera.Status.ONLINE, Camera.Status.OFFLINE, Camera.Status.ERROR})
