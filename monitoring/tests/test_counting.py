"""Counting engine: occupancy, unique birds, line crossings and zones."""

from __future__ import annotations

from django.test import SimpleTestCase

from monitoring.ml.counting import CountingEngine, ZoneSpec
from monitoring.ml.tracker import ActiveTrack


def track(track_id: int, bbox, hits: int = 1) -> ActiveTrack:
    return ActiveTrack(track_id=track_id, bbox=bbox, confidence=0.9, hits=hits, age=0)


def box(cx: float, cy: float, size: float = 20.0):
    return (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2)


WIDTH, HEIGHT = 640, 480


class OccupancyTests(SimpleTestCase):
    def test_visible_track_is_counted_once(self):
        engine = CountingEngine(min_hits=1)
        snapshots = [
            engine.update(i, i * 0.5, [track(1, box(100, 100))], WIDTH, HEIGHT) for i in range(4)
        ]
        self.assertEqual(snapshots[-1].unique_total, 1)
        self.assertEqual(snapshots[-1].occupancy, 1)
        self.assertEqual(engine.peak_occupancy, 1)

    def test_min_hits_filters_flicker(self):
        engine = CountingEngine(min_hits=3)
        for i in range(2):
            snap = engine.update(i, i * 0.5, [track(1, box(100, 100), hits=1)], WIDTH, HEIGHT)
        self.assertEqual(snap.unique_total, 0)
        self.assertEqual(snap.occupancy, 0)
        snap = engine.update(2, 1.0, [track(1, box(100, 100), hits=3)], WIDTH, HEIGHT)
        self.assertEqual(snap.unique_total, 1)

    def test_two_boxes_are_two_birds(self):
        engine = CountingEngine(min_hits=1)
        snap = engine.update(
            0, 0.0, [track(1, box(100, 100)), track(2, box(500, 100))], WIDTH, HEIGHT
        )
        self.assertEqual(snap.unique_total, 2)
        self.assertEqual(snap.occupancy, 2)

    def test_missing_track_stays_within_grace_window(self):
        engine = CountingEngine(min_hits=1, occupancy_grace_frames=2)
        engine.update(0, 0.0, [track(1, box(100, 100))], WIDTH, HEIGHT)
        snap = engine.update(1, 0.5, [], WIDTH, HEIGHT)
        self.assertEqual(snap.occupancy, 1, "a one-frame dropout should not empty the house")
        snap = engine.update(2, 1.0, [], WIDTH, HEIGHT)
        self.assertEqual(snap.occupancy, 1)
        snap = engine.update(3, 1.5, [], WIDTH, HEIGHT)
        self.assertEqual(snap.occupancy, 0, "beyond the grace window the house is empty")

    def test_reacquired_bird_is_not_counted_twice(self):
        engine = CountingEngine(
            min_hits=1,
            max_age_frames=2,
            dedupe_window_s=10.0,
            dedupe_distance_norm=0.1,
        )
        engine.update(0, 0.0, [track(1, box(300, 300))], WIDTH, HEIGHT)
        for frame in (1, 2, 3, 4, 5):
            engine.update(frame, frame * 0.5, [], WIDTH, HEIGHT)
        self.assertEqual(engine.unique_total, 1)

        snap = engine.update(6, 3.0, [track(2, box(305, 302))], WIDTH, HEIGHT)
        self.assertEqual(snap.unique_total, 1, "the same bird reappeared; do not double count")
        self.assertEqual(snap.new_roots, [])

    def test_bird_returning_much_later_is_new(self):
        engine = CountingEngine(
            min_hits=1,
            max_age_frames=2,
            dedupe_window_s=1.0,
            dedupe_distance_norm=0.02,
        )
        engine.update(0, 0.0, [track(1, box(300, 300))], WIDTH, HEIGHT)
        for frame in (1, 2, 3):
            engine.update(frame, frame * 0.5, [], WIDTH, HEIGHT)
        snap = engine.update(50, 60.0, [track(2, box(300, 300))], WIDTH, HEIGHT)
        self.assertEqual(snap.unique_total, 2)


class LineCrossingTests(SimpleTestCase):
    LINE = [(0, HEIGHT / 2), (WIDTH, HEIGHT / 2)]

    def _walk(self, engine: CountingEngine, ys, start_frame: int = 0):
        snap = None
        for offset, y in enumerate(ys):
            frame = start_frame + offset
            snap = engine.update(frame, frame * 0.2, [track(1, box(320, y))], WIDTH, HEIGHT)
        return snap

    def test_downward_crossing_counts_in(self):
        engine = CountingEngine(min_hits=1, line=self.LINE, hysteresis=3)
        snap = self._walk(engine, [100, 150, 200, 280, 300, 320, 360, 400])
        self.assertEqual(snap.line_in, 1)
        self.assertEqual(snap.line_out, 0)

    def test_upward_crossing_counts_out(self):
        engine = CountingEngine(min_hits=1, line=self.LINE, hysteresis=3)
        snap = self._walk(engine, [400, 360, 320, 300, 280, 200, 150, 100])
        self.assertEqual(snap.line_out, 1)
        self.assertEqual(snap.line_in, 0)

    def test_single_frame_wobble_is_ignored(self):
        engine = CountingEngine(min_hits=1, line=self.LINE, hysteresis=3)
        snap = self._walk(engine, [100, 100, 300, 100, 100, 100])
        self.assertEqual(snap.line_in + snap.line_out, 0)

    def test_inverted_line_swaps_direction(self):
        engine = CountingEngine(min_hits=1, line=self.LINE, hysteresis=3, invert_line=True)
        snap = self._walk(engine, [100, 150, 200, 280, 300, 320])
        self.assertEqual(snap.line_out, 1)
        self.assertEqual(snap.line_in, 0)

    def test_crossing_back_counts_again(self):
        engine = CountingEngine(min_hits=1, line=self.LINE, hysteresis=3)
        snap = self._walk(engine, [100, 200, 300, 320, 340, 400], start_frame=0)
        snap = self._walk(engine, [400, 340, 320, 300, 200, 100, 80], start_frame=10)
        self.assertEqual(snap.line_in, 1)
        self.assertEqual(snap.line_out, 1)


class ZoneTests(SimpleTestCase):
    def test_zone_occupancy(self):
        zones = [
            ZoneSpec(
                name="feeder",
                kind="feeder",
                polygon=[(0, 0), (200, 0), (200, 200), (0, 200)],
            )
        ]
        engine = CountingEngine(min_hits=1, zones=zones)
        snap = engine.update(
            0, 0.0, [track(1, box(100, 100)), track(2, box(500, 100))], WIDTH, HEIGHT
        )
        self.assertEqual(snap.zone_counts["feeder"], 1)
        summary = {zone["name"]: zone for zone in engine.zone_summary()}
        self.assertEqual(summary["feeder"]["peak"], 1)
