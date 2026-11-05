"""Counting logic layered on top of the tracker.

Responsibilities
----------------
* **Occupancy** – how many birds are visible in the frame right now.
* **Unique birds** – how many distinct birds have been seen, with a short-term
  re-identification step that merges a lost-and-reacquired trajectory instead of
  counting the same chicken twice.
* **Directional movement** – IN/OUT events for a counting line.
* **Zone occupancy** – average/peak birds inside named areas (feeder, drinker...).

All thresholds are in *processed frames or seconds*, never wall-clock, so results
are reproducible for a given video.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .geometry import (
    BBox,
    Point,
    bbox_center,
    bbox_foot,
    euclidean,
    line_projection,
    line_side,
    point_in_polygon,
)
from .tracker import ActiveTrack


@dataclass
class TrackState:
    track_id: int
    root_id: int
    bbox: BBox
    centroid: Point
    foot: Point
    confidence: float
    frames_seen: int
    first_frame: int
    last_frame: int
    first_seen_s: float
    last_seen_s: float
    missing: int = 0
    counted: bool = False
    side: float | None = None
    pending_side: float | None = None
    pending_frames: int = 0


@dataclass(slots=True)
class CountingEvent:
    kind: str  # "in" | "out"
    track_id: int
    root_id: int
    frame_index: int
    timestamp_s: float
    point: Point


@dataclass(slots=True)
class Snapshot:
    frame_index: int
    timestamp_s: float
    occupancy: int
    unique_total: int
    line_in: int
    line_out: int
    zone_counts: dict[str, int]
    tracks: list[TrackState] = field(default_factory=list)
    events: list[CountingEvent] = field(default_factory=list)
    new_roots: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ZoneSpec:
    name: str
    kind: str
    polygon: Sequence[Point]


class CountingEngine:
    def __init__(
        self,
        *,
        min_hits: int = 5,
        line: Sequence[Point] | None = None,
        invert_line: bool = False,
        zones: Iterable[ZoneSpec] = (),
        max_age_frames: int = 45,
        dedupe_window_s: float = 6.0,
        dedupe_distance_norm: float = 0.06,
        hysteresis: int = 3,
        occupancy_grace_frames: int = 10,
    ) -> None:
        self.min_hits = max(1, min_hits)
        self.line = list(line) if line else None
        self.invert_line = invert_line
        self.zones = list(zones)
        self.max_age_frames = max(1, max_age_frames)
        self.dedupe_window_s = max(0.0, dedupe_window_s)
        self.dedupe_distance_norm = dedupe_distance_norm
        self.hysteresis = max(1, hysteresis)
        self.occupancy_grace_frames = max(0, occupancy_grace_frames)

        self._states: dict[int, TrackState] = {}
        self._retired: list[TrackState] = []
        self._roots: dict[int, TrackState] = {}
        self._peak_occupancy = 0
        self._occupancy_sum = 0
        self._occupancy_samples = 0
        self._zone_peak: dict[str, int] = {z.name: 0 for z in self.zones}
        self._zone_sum: dict[str, float] = {z.name: 0.0 for z in self.zones}
        self._zone_samples: dict[str, int] = {z.name: 0 for z in self.zones}
        self.line_in = 0
        self.line_out = 0
        self.frames_seen = 0

    # ------------------------------------------------------------------ state
    @property
    def unique_total(self) -> int:
        return len(self._roots)

    @property
    def peak_occupancy(self) -> int:
        return self._peak_occupancy

    @property
    def average_occupancy(self) -> float:
        if not self._occupancy_samples:
            return 0.0
        return self._occupancy_sum / self._occupancy_samples

    def prime_from_tracks(self, tracks: Iterable[ActiveTrack], frame_index: int, timestamp_s: float):
        """Re-seed state after a worker restart (best effort)."""
        for track in tracks:
            self._touch(track, frame_index, timestamp_s)

    # ----------------------------------------------------------------- update
    def update(
        self,
        frame_index: int,
        timestamp_s: float,
        tracks: Sequence[ActiveTrack],
        width: int,
        height: int,
    ) -> Snapshot:
        self.frames_seen += 1
        current_ids: set[int] = set()
        events: list[CountingEvent] = []
        new_roots: list[int] = []

        for track in tracks:
            if not track.is_current:
                continue
            state = self._touch(track, frame_index, timestamp_s)
            current_ids.add(track.track_id)

            if not state.counted and state.frames_seen >= self.min_hits:
                root_id = self._reidentify(state, frame_index, timestamp_s, width, height)
                state.root_id = root_id
                state.counted = True
                if root_id not in self._roots:
                    self._roots[root_id] = state
                    new_roots.append(root_id)
                else:
                    self._roots[root_id] = state

            if state.counted and self.line is not None:
                event = self._check_line(state)
                if event is not None:
                    events.append(event)

        # Retire tracks that the tracker has not reported for a while.
        for track_id in [tid for tid in self._states if tid not in current_ids]:
            state = self._states[track_id]
            state.missing += 1
            if state.missing > self.max_age_frames:
                self._retired.append(self._states.pop(track_id))
        self._prune_retired()

        active = [
            s
            for s in self._states.values()
            if s.counted and s.missing <= self.occupancy_grace_frames
        ]
        occupancy = len(active)
        self._peak_occupancy = max(self._peak_occupancy, occupancy)
        self._occupancy_sum += occupancy
        self._occupancy_samples += 1

        zone_counts = {zone.name: 0 for zone in self.zones}
        for state in active:
            for zone in self.zones:
                if point_in_polygon(state.foot, zone.polygon):
                    zone_counts[zone.name] += 1
        for name, count in zone_counts.items():
            self._zone_peak[name] = max(self._zone_peak[name], count)
            self._zone_sum[name] += count
            self._zone_samples[name] += 1

        return Snapshot(
            frame_index=frame_index,
            timestamp_s=timestamp_s,
            occupancy=occupancy,
            unique_total=self.unique_total,
            line_in=self.line_in,
            line_out=self.line_out,
            zone_counts=zone_counts,
            tracks=active,
            events=events,
            new_roots=new_roots,
        )

    # ------------------------------------------------------------- internals
    def _touch(self, track: ActiveTrack, frame_index: int, timestamp_s: float) -> TrackState:
        state = self._states.get(track.track_id)
        if state is None:
            state = TrackState(
                track_id=track.track_id,
                root_id=track.track_id,
                bbox=track.bbox,
                centroid=bbox_center(track.bbox),
                foot=bbox_foot(track.bbox),
                confidence=track.confidence,
                frames_seen=max(1, track.hits),
                first_frame=frame_index,
                last_frame=frame_index,
                first_seen_s=timestamp_s,
                last_seen_s=timestamp_s,
            )
            self._states[track.track_id] = state
            return state
        state.bbox = track.bbox
        state.centroid = bbox_center(track.bbox)
        state.foot = bbox_foot(track.bbox)
        state.confidence = track.confidence
        state.frames_seen = max(state.frames_seen + 1, track.hits)
        state.last_frame = frame_index
        state.last_seen_s = timestamp_s
        state.missing = 0
        return state

    def _reidentify(
        self, state: TrackState, frame_index: int, timestamp_s: float, width: int, height: int
    ) -> int:
        """Return the root id this new trajectory belongs to (or itself)."""
        if not self._roots:
            return state.track_id
        diagonal = max(1.0, float((width * width + height * height) ** 0.5))
        best_root: int | None = None
        best_distance = float("inf")
        for root_id, candidate in self._roots.items():
            if root_id == state.track_id:
                continue
            # Never merge two trajectories that are on screen at the same time.
            if candidate.missing == 0 and candidate.last_frame == frame_index:
                continue
            gap = timestamp_s - candidate.last_seen_s
            if gap < 0 or gap > self.dedupe_window_s:
                continue
            distance = euclidean(state.foot, candidate.foot) / diagonal
            if distance <= self.dedupe_distance_norm and distance < best_distance:
                best_distance = distance
                best_root = root_id
        return best_root if best_root is not None else state.track_id

    def _check_line(self, state: TrackState) -> CountingEvent | None:
        assert self.line is not None
        side = line_side(state.centroid, self.line)
        if state.side is None:
            state.side = side
            return None
        if side * state.side >= 0:
            state.pending_frames = 0
            state.pending_side = None
            return None

        if state.pending_side is not None and state.pending_side * side > 0:
            state.pending_frames += 1
        else:
            state.pending_side = side
            state.pending_frames = 1
        if state.pending_frames < self.hysteresis:
            return None

        projection = line_projection(state.centroid, self.line)
        state.side = side
        state.pending_frames = 0
        state.pending_side = None
        if not -0.03 <= projection <= 1.03:
            return None

        kind = "in" if side > 0 else "out"
        if self.invert_line:
            kind = "out" if kind == "in" else "in"
        if kind == "in":
            self.line_in += 1
        else:
            self.line_out += 1
        return CountingEvent(
            kind=kind,
            track_id=state.track_id,
            root_id=state.root_id,
            frame_index=state.last_frame,
            timestamp_s=state.last_seen_s,
            point=state.centroid,
        )

    def _prune_retired(self) -> None:
        if len(self._retired) > 20_000:
            del self._retired[:5_000]

    # --------------------------------------------------------------- reporting
    def zone_summary(self) -> list[dict]:
        summary = []
        for zone in self.zones:
            samples = self._zone_samples.get(zone.name, 0)
            summary.append(
                {
                    "name": zone.name,
                    "kind": zone.kind,
                    "peak": self._zone_peak.get(zone.name, 0),
                    "average": (self._zone_sum.get(zone.name, 0.0) / samples)
                    if samples
                    else 0.0,
                }
            )
        return summary

    def track_records(self) -> list[dict]:
        """Aggregate tracker trajectories into one record per unique bird (root)."""
        records: dict[int, dict] = {}

        def absorb(state: TrackState) -> None:
            root_id = state.root_id if state.counted else state.track_id
            record = records.get(root_id)
            if record is None:
                records[root_id] = {
                    "root_id": root_id,
                    "first_frame": state.first_frame,
                    "last_frame": state.last_frame,
                    "first_seen_s": state.first_seen_s,
                    "last_seen_s": state.last_seen_s,
                    "frames_seen": state.frames_seen,
                    "conf_sum": state.confidence * state.frames_seen,
                    "weight": state.frames_seen,
                    "is_counted": state.counted,
                    "last_bbox": list(state.bbox),
                }
                return
            if state.first_frame < record["first_frame"]:
                record["first_frame"] = state.first_frame
                record["first_seen_s"] = state.first_seen_s
            if state.last_frame > record["last_frame"]:
                record["last_frame"] = state.last_frame
                record["last_seen_s"] = state.last_seen_s
                record["last_bbox"] = list(state.bbox)
            record["frames_seen"] += state.frames_seen
            record["conf_sum"] += state.confidence * state.frames_seen
            record["weight"] += state.frames_seen
            record["is_counted"] = record["is_counted"] or state.counted

        for state in self._states.values():
            absorb(state)
        for state in self._retired:
            absorb(state)

        result = []
        for record in records.values():
            weight = record.pop("weight") or 1
            record["confidence"] = min(0.999, record.pop("conf_sum") / weight)
            result.append(record)
        result.sort(key=lambda item: item["root_id"])
        return result

    def summary(self) -> dict:
        return {
            "unique_birds": self.unique_total,
            "peak_occupancy": self._peak_occupancy,
            "average_occupancy": round(self.average_occupancy, 2),
            "line_in": self.line_in,
            "line_out": self.line_out,
            "zone_stats": self.zone_summary(),
        }
