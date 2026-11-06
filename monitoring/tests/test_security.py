"""Access control, throttling, CSRF and security headers."""

from __future__ import annotations

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from monitoring.models import Camera, VideoAsset


def make_user(username: str, role: str) -> User:
    user = User.objects.create_user(username=username, password="Test-passw0rd-2026")
    user.role = role
    user.save(update_fields=["role"])
    return user


class AuthTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = make_user("admin", User.Role.ADMIN)
        self.operator = make_user("operator", User.Role.OPERATOR)
        self.viewer = make_user("viewer", User.Role.VIEWER)

    def test_anonymous_users_are_redirected_to_login(self):
        response = self.client.get(reverse("monitoring:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_health_check_is_public(self):
        response = self.client.get(reverse("monitoring:healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["database"], True)

    def test_login_success_and_failure(self):
        response = self.client.post(
            reverse("login"), {"username": "viewer", "password": "Test-passw0rd-2026"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/", response["Location"])

        self.client.logout()
        response = self.client.post(reverse("login"), {"username": "viewer", "password": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Incorrect username or password")

    def test_login_is_rate_limited(self):
        for _ in range(8):
            response = self.client.post(
                reverse("login"), {"username": "viewer", "password": "wrong-password"}
            )
            self.assertEqual(response.status_code, 200)
        response = self.client.post(
            reverse("login"), {"username": "viewer", "password": "wrong-password"}
        )
        self.assertEqual(response.status_code, 429)

    def test_logout_requires_post(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("logout"))
        self.assertEqual(response.status_code, 405)

        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 302)


class RoleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = make_user("admin2", User.Role.ADMIN)
        self.operator = make_user("operator2", User.Role.OPERATOR)
        self.viewer = make_user("viewer2", User.Role.VIEWER)
        self.camera = Camera.objects.create(name="Test house", stream_url="rtsp://cam/stream")

    def test_viewer_can_read_but_not_write(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("monitoring:dashboard")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("monitoring:camera_detail", args=[self.camera.pk])).status_code,
            200,
        )
        self.assertEqual(self.client.get(reverse("monitoring:video_upload")).status_code, 403)

    def test_operator_can_upload_but_not_read_audit(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("monitoring:video_upload")).status_code, 200)
        self.assertEqual(self.client.get(reverse("monitoring:audit")).status_code, 403)

    def test_admin_can_read_audit(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("monitoring:audit")).status_code, 200)

    def test_viewer_cannot_start_a_camera(self):
        self.client.force_login(self.viewer)
        response = self.client.post(reverse("monitoring:camera_start", args=[self.camera.pk]))
        self.assertEqual(response.status_code, 403)

    def test_private_media_requires_authentication(self):
        response = self.client.get(reverse("monitoring:run_video", args=[1]))
        self.assertEqual(response.status_code, 302)

    def test_security_headers_are_present(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("monitoring:dashboard"))
        csp = response["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertIn("camera=()", response["Permissions-Policy"])


class CsrfTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = make_user("admin3", User.Role.ADMIN)
        self.camera = Camera.objects.create(name="CSRF house", stream_url="rtsp://cam/stream")

    def test_state_changing_post_without_token_is_rejected(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.admin)
        response = client.post(reverse("monitoring:camera_start", args=[self.camera.pk]))
        self.assertEqual(response.status_code, 403)

    def test_post_with_token_is_accepted(self):
        self.client.force_login(self.admin)
        self.client.get(reverse("monitoring:dashboard"))  # sets the CSRF cookie
        token = self.client.cookies["csrftoken"].value
        response = self.client.post(
            reverse("monitoring:camera_start", args=[self.camera.pk]),
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 302)


class UploadValidationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.manager = make_user("manager", User.Role.OPERATOR)
        self.client.force_login(self.manager)

    def test_non_video_extension_is_rejected(self):
        upload = SimpleUploadedFile("notes.txt", b"this is not a video", content_type="text/plain")
        response = self.client.post(reverse("monitoring:video_upload"), {"video": upload})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(VideoAsset.objects.count(), 0)

    def test_renamed_image_is_rejected_by_probe(self):
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
            b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
            b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        upload = SimpleUploadedFile("clip.mp4", png, content_type="video/mp4")
        response = self.client.post(reverse("monitoring:video_upload"), {"video": upload})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(VideoAsset.objects.count(), 0)

    @override_settings(CHICKEN_MAX_UPLOAD_MB=0)
    def test_size_limit_is_enforced(self):
        upload = SimpleUploadedFile("clip.mp4", b"x" * 2048, content_type="video/mp4")
        response = self.client.post(reverse("monitoring:video_upload"), {"video": upload})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(VideoAsset.objects.count(), 0)
