"""Service-layer rules around camera sessions, retries and alerts."""

from __future__ import annotations

from django.test import TestCase

from accounts.models import User
from monitoring.models import Alert, Camera, DetectionRun, VideoAsset
from monitoring.services import (
    ServiceError,
    acknowledge_alert,
    create_video_run,
    retry_run,
    start_camera_run,
    stop_camera_run,
)


class CameraSessionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="operator", password="Test-passw0rd-2026")

    def test_camera_without_stream_cannot_start(self):
        camera = Camera.objects.create(name="No stream")
        with self.assertRaises(ServiceError):
            start_camera_run(camera, self.user)

    def test_disabled_camera_cannot_start(self):
        camera = Camera.objects.create(name="Disabled", stream_url="rtsp://cam/1", is_active=False)
        with self.assertRaises(ServiceError):
            start_camera_run(camera, self.user)

    def test_start_then_stop(self):
        camera = Camera.objects.create(name="Live", stream_url="rtsp://cam/1")
        run = start_camera_run(camera, self.user)
        self.assertEqual(run.status, DetectionRun.Status.QUEUED)
        camera.refresh_from_db()
        self.assertEqual(camera.status, Camera.Status.STARTING)

        with self.assertRaises(ServiceError):
            start_camera_run(camera, self.user)

        stopped = stop_camera_run(camera)
        self.assertIsNotNone(stopped)
        stopped.refresh_from_db()
        self.assertTrue(stopped.cancel_requested)

    def test_stop_without_session_returns_none(self):
        camera = Camera.objects.create(name="Idle", stream_url="rtsp://cam/1")
        self.assertIsNone(stop_camera_run(camera))


class RunRetryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="operator2", password="Test-passw0rd-2026")
        self.video = VideoAsset.objects.create(
            file="private/videos/sample.mp4",
            original_name="sample.mp4",
            size_bytes=1024,
        )

    def test_video_run_is_requeued_with_the_same_source(self):
        run = create_video_run(self.video, self.user, title="Morning")
        run.status = DetectionRun.Status.FAILED
        run.save(update_fields=["status"])

        retry = retry_run(run, self.user)
        self.assertEqual(retry.status, DetectionRun.Status.QUEUED)
        self.assertEqual(retry.video_id, self.video.pk)
        self.assertIn("retry", retry.title)

    def test_retry_without_video_raises(self):
        run = create_video_run(self.video, self.user)
        run.video = None
        run.save(update_fields=["video"])
        with self.assertRaises(ServiceError):
            retry_run(run, self.user)


class AlertTests(TestCase):
    def test_acknowledge_is_idempotent(self):
        user = User.objects.create_user(username="ack", password="Test-passw0rd-2026")
        alert = Alert.objects.create(kind=Alert.Kind.OVERCROWDING, message="Too many birds")
        acknowledge_alert(alert, user)
        acknowledge_alert(alert, user)
        alert.refresh_from_db()
        self.assertTrue(alert.is_acknowledged)
        self.assertEqual(alert.acknowledged_by_id, user.pk)
        self.assertIsNotNone(alert.acknowledged_at)
