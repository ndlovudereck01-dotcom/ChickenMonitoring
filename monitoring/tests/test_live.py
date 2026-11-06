"""Live frame and MJPEG endpoints."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User

JPEG = b"\xff\xd8\xff\xe0" + b"annotated-frame" * 8 + b"\xff\xd9"


class LiveEndpointsTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chickenvision-live-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.override = override_settings(LIVE_FRAME_ROOT=self.tmp)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user = User.objects.create_user(username="watcher", password="Test-passw0rd-2026")

    def _publish(self, camera_id: int = 1) -> Path:
        path = self.tmp / f"{camera_id}.jpg"
        path.write_bytes(JPEG)
        return path

    def test_frame_requires_authentication(self):
        self._publish()
        response = self.client.get(reverse("monitoring:live_frame", args=[1]))
        self.assertEqual(response.status_code, 302)

    def test_frame_is_served_without_caching(self):
        self._publish()
        self.client.force_login(self.user)
        response = self.client.get(reverse("monitoring:live_frame", args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response.content, JPEG)

    def test_missing_frame_is_404(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("monitoring:live_frame", args=[99]))
        self.assertEqual(response.status_code, 404)

    def test_mjpeg_stream_starts_with_the_newest_frame(self):
        from monitoring.models import Camera

        camera = Camera.objects.create(name="Stream camera")
        self._publish(camera.pk)
        self.client.force_login(self.user)
        response = self.client.get(reverse("monitoring:live_stream", args=[camera.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("multipart/x-mixed-replace", response["Content-Type"])
        chunk = next(iter(response.streaming_content))
        self.assertIn(b"Content-Type: image/jpeg", chunk)
        self.assertIn(JPEG, chunk)
        response.close()
