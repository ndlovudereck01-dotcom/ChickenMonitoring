"""Training-data review endpoints: access control, label round-trip, path safety."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User


def make_user(username: str, role: str) -> User:
    user = User.objects.create_user(username=username, password="Test-passw0rd-2026")
    user.role = role
    user.save(update_fields=["role"])
    return user


class DatasetReviewTests(TestCase):
    def setUp(self):
        from PIL import Image

        self.tmp = Path(tempfile.mkdtemp(prefix="chickenvision-labels-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "images" / "train").mkdir(parents=True)
        (self.tmp / "images" / "val").mkdir(parents=True)
        (self.tmp / "labels" / "train").mkdir(parents=True)
        (self.tmp / "labels" / "val").mkdir(parents=True)

        Image.new("RGB", (64, 48), (90, 120, 80)).save(self.tmp / "images" / "train" / "coop_001.jpg")
        (self.tmp / "labels" / "train" / "coop_001.txt").write_text(
            "0 0.500000 0.500000 0.200000 0.200000\n", encoding="utf-8"
        )

        self.override = override_settings(CHICKEN_DATASET_DIR=self.tmp)
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.admin = make_user("labeladmin", User.Role.ADMIN)
        self.operator = make_user("labeloperator", User.Role.OPERATOR)
        self.viewer = make_user("labelviewer", User.Role.VIEWER)

    # ------------------------------------------------------------------ access
    def test_anonymous_users_are_redirected(self):
        response = self.client.get(reverse("monitoring:dataset_review"))
        self.assertEqual(response.status_code, 302)

    def test_viewers_cannot_open_or_read_the_dataset(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("monitoring:dataset_review")).status_code, 403)
        self.assertEqual(self.client.get(reverse("monitoring:api_dataset_images")).status_code, 403)
        response = self.client.get(
            reverse("monitoring:dataset_image", args=["train", "coop_001"])
        )
        self.assertEqual(response.status_code, 403)

    def test_operators_see_the_page(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("monitoring:dataset_review"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "label-canvas")

    # -------------------------------------------------------------------- data
    def test_image_list_includes_boxes_and_review_state(self):
        self.client.force_login(self.operator)
        payload = self.client.get(reverse("monitoring:api_dataset_images")).json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["images"], 1)
        self.assertEqual(payload["summary"]["pending"], 1)
        item = payload["images"][0]
        self.assertEqual(item["split"], "train")
        self.assertEqual(item["stem"], "coop_001")
        self.assertEqual(item["width"], 64)
        self.assertEqual(len(item["boxes"]), 1)
        self.assertFalse(item["reviewed"])
        self.assertEqual(
            item["url"], reverse("monitoring:dataset_image", args=["train", "coop_001"])
        )

    def test_image_bytes_are_served_to_operators(self):
        self.client.force_login(self.operator)
        response = self.client.get(
            reverse("monitoring:dataset_image", args=["train", "coop_001"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertIn("private", response["Cache-Control"])

    def test_unknown_image_is_404(self):
        self.client.force_login(self.operator)
        response = self.client.get(
            reverse("monitoring:dataset_image", args=["train", "does_not_exist"])
        )
        self.assertEqual(response.status_code, 404)

    def test_saving_boxes_rewrites_the_label_file(self):
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("monitoring:api_dataset_save"),
            data=json.dumps(
                {
                    "split": "train",
                    "stem": "coop_001",
                    "reviewed": True,
                    "boxes": [[0.25, 0.5, 0.1, 0.2], [0.75, 0.5, 0.2, 0.3]],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(response.json()["reviewed"])
        self.assertEqual(response.json()["summary"]["reviewed"], 1)

        written = (self.tmp / "labels" / "train" / "coop_001.txt").read_text(encoding="utf-8")
        self.assertIn("0 0.250000 0.500000 0.100000 0.200000", written)
        self.assertIn("0 0.750000 0.500000 0.200000 0.300000", written)
        state = json.loads((self.tmp / "review_state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["train/coop_001"]["reviewed"])

    def test_approving_an_empty_frame_keeps_it_as_a_negative_sample(self):
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("monitoring:api_dataset_save"),
            data=json.dumps(
                {"split": "train", "stem": "coop_001", "reviewed": True, "boxes": []}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (self.tmp / "labels" / "train" / "coop_001.txt").read_text(encoding="utf-8"), ""
        )

    def test_out_of_range_boxes_are_rejected(self):
        self.client.force_login(self.operator)
        before = (self.tmp / "labels" / "train" / "coop_001.txt").read_text(encoding="utf-8")
        response = self.client.post(
            reverse("monitoring:api_dataset_save"),
            data=json.dumps(
                {"split": "train", "stem": "coop_001", "boxes": [[1.4, 0.5, 0.2, 0.2]]}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(
            (self.tmp / "labels" / "train" / "coop_001.txt").read_text(encoding="utf-8"), before
        )

    def test_path_traversal_is_refused(self):
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("monitoring:api_dataset_save"),
            data=json.dumps(
                {"split": "train", "stem": "../../settings", "boxes": []}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_split_is_refused(self):
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("monitoring:api_dataset_save"),
            data=json.dumps({"split": "test", "stem": "coop_001", "boxes": []}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_save_requires_csrf(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.operator)
        response = client.post(
            reverse("monitoring:api_dataset_save"),
            data=json.dumps({"split": "train", "stem": "coop_001", "boxes": []}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
