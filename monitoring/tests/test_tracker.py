"""Tracker behaviour, including the IoU fallback and its id namespace."""

from __future__ import annotations

import numpy as np
from django.test import SimpleTestCase
from unittest import mock

from monitoring.ml.detector import Detection
from monitoring.ml.tracker import HybridTracker, IoUTracker, build_tracker

FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


def detection(cx: float, cy: float, size: float = 24.0) -> Detection:
    return Detection(
        bbox=(cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2),
        confidence=0.9,
        class_id=14,
        label="bird",
    )


class IoUTrackerTests(SimpleTestCase):
    def test_keeps_one_id_for_a_moving_bird(self):
        tracker = IoUTracker(n_init=1)
        ids = set()
        for step in range(6):
            tracks = tracker.update([detection(100 + step * 6, 200)], FRAME)
            if tracks:
                ids.add(tracks[0].track_id)
        self.assertEqual(ids, {1})

    def test_separate_birds_get_separate_ids(self):
        tracker = IoUTracker(n_init=1)
        tracks = tracker.update([detection(100, 100), detection(500, 300)], FRAME)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(len({t.track_id for t in tracks}), 2)

    def test_id_offset_keeps_fallback_ids_apart(self):
        tracker = IoUTracker(n_init=1, id_offset=900_000)
        tracks = tracker.update([detection(100, 100)], FRAME)
        self.assertGreaterEqual(tracks[0].track_id, 900_000)

    def test_n_init_delays_reporting(self):
        tracker = IoUTracker(n_init=3)
        self.assertEqual(tracker.update([detection(100, 100)], FRAME), [])
        self.assertEqual(tracker.update([detection(100, 100)], FRAME), [])
        self.assertEqual(len(tracker.update([detection(100, 100)], FRAME)), 1)

    def test_empty_frame_does_not_crash(self):
        tracker = IoUTracker(n_init=1)
        tracker.update([], FRAME)
        self.assertEqual(tracker.update([], FRAME), [])


class HybridTests(SimpleTestCase):
    def test_falls_back_when_deepsort_init_fails(self):
        with mock.patch(
            "monitoring.ml.tracker.DeepSortAdapter", side_effect=RuntimeError("no embedder")
        ):
            tracker = HybridTracker(n_init=1)
        self.assertTrue(tracker.degraded)
        self.assertEqual(tracker.backend, "iou")
        self.assertEqual(tracker.fallback_reason, "deepsort_init_failed")
        tracks = tracker.update([detection(100, 100)], FRAME)
        self.assertEqual(len(tracks), 1)

    def test_degrades_when_deepsort_crashes_mid_run(self):
        with mock.patch("monitoring.ml.tracker.DeepSortAdapter") as adapter_cls:
            adapter = adapter_cls.return_value
            adapter.update.side_effect = RuntimeError("tracker blew up")
            tracker = HybridTracker(n_init=1)
            self.assertEqual(tracker.backend, "deepsort")
            tracks = tracker.update([detection(100, 100)], FRAME)
        self.assertTrue(tracker.degraded)
        self.assertEqual(tracker.backend, "iou")
        self.assertEqual(len(tracks), 1)

    def test_build_tracker_returns_hybrid(self):
        tracker = build_tracker()
        self.assertIsInstance(tracker, HybridTracker)
