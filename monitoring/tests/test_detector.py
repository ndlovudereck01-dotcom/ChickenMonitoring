"""Detector weight resolution and class selection."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from monitoring.ml.detector import Detector, DetectorError, resolve_class_ids


class ClassResolutionTests(SimpleTestCase):
    def test_poultry_names_win_over_coco_bird(self):
        names = {0: "chicken", 14: "bird", 1: "person"}
        self.assertEqual(resolve_class_ids(names), [0])

    def test_falls_back_to_coco_bird(self):
        names = {14: "bird", 1: "person"}
        self.assertEqual(resolve_class_ids(names), [14])

    def test_unknown_checkpoint_keeps_every_class(self):
        self.assertIsNone(resolve_class_ids({0: "widget"}, ""))

    def test_explicit_class_ids(self):
        self.assertEqual(resolve_class_ids({0: "a", 1: "b"}, "0, 1"), [0, 1])
        with self.assertRaises(DetectorError):
            resolve_class_ids({0: "a"}, "chicken")


class WeightFallbackTests(SimpleTestCase):
    def test_missing_custom_checkpoint_falls_back_to_yolo11n(self):
        missing = Path(tempfile.gettempdir()) / "does-not-exist" / "chicken_yolo11.pt"
        fake_model = mock.MagicMock()
        fake_model.names = {14: "bird"}
        with mock.patch("ultralytics.YOLO", return_value=fake_model) as yolo:
            detector = Detector(str(missing), torch_threads=1)
        yolo.assert_called_once_with("yolo11n.pt")
        self.assertEqual(detector.describe, "yolo11n.pt")
        self.assertEqual(detector.class_ids, [14])

    def test_official_checkpoint_name_is_passed_through(self):
        fake_model = mock.MagicMock()
        fake_model.names = {14: "bird"}
        with mock.patch("ultralytics.YOLO", return_value=fake_model) as yolo:
            Detector("yolo11n.pt", torch_threads=1)
        yolo.assert_called_once_with("yolo11n.pt")
