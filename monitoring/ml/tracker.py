"""Multi-object tracking: DeepSORT with a self-contained IoU fallback.

DeepSORT gives appearance-aware re-identification across occlusions, which is what
keeps the unique-bird count stable in a crowded house. Its CNN embedder needs
torchvision weights, so a lightweight IoU tracker is kept as a safety net: if the
embedder cannot be created or crashes at runtime, a run degrades gracefully
instead of failing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from .detector import Detection
from .geometry import BBox, Point, bbox_center, bbox_iou, euclidean

logger = logging.getLogger("monitoring.ml.tracker")


@dataclass(slots=True)
class ActiveTrack:
    track_id: int
    bbox: BBox
    confidence: float
    hits: int
    age: int
    is_current: bool = True


class _BaseTracker:
    degraded = False

    def update(self, detections: list[Detection], frame: np.ndarray) -> list[ActiveTrack]:
        raise NotImplementedError


class DeepSortAdapter(_BaseTracker):
    """Wraps ``deep_sort_realtime.deepsort_tracker.DeepSort``."""

    def __init__(
        self,
        *,
        max_age: int = 45,
        n_init: int = 3,
        max_cosine_distance: float = 0.35,
        embedder: str | None = "mobilenet",
        id_offset: int = 0,
    ) -> None:
        from deep_sort_realtime.deepsort_tracker import DeepSort

        self._tracker = DeepSort(
            max_age=max_age,
            n_init=n_init,
            max_cosine_distance=max_cosine_distance,
            nn_budget=100,
            embedder=embedder,
            half=False,
            bgr=True,
            embedder_gpu=False,
            embedder_model_name=None,
        )
        self.id_offset = id_offset

    def update(self, detections: list[Detection], frame: np.ndarray) -> list[ActiveTrack]:
        raw = [
            ([d.bbox[0], d.bbox[1], d.bbox[2] - d.bbox[0], d.bbox[3] - d.bbox[1]], d.confidence, d.label)
            for d in detections
        ]
        tracks = self._tracker.update_tracks(raw, frame=frame)
        output: list[ActiveTrack] = []
        for track in tracks:
            if not track.is_confirmed():
                continue
            x1, y1, x2, y2 = (float(v) for v in track.to_tlbr())
            if x2 <= x1 or y2 <= y1:
                continue
            output.append(
                ActiveTrack(
                    track_id=int(track.track_id) + self.id_offset,
                    bbox=(x1, y1, x2, y2),
                    confidence=float(track.det_conf if track.det_conf is not None else 0.5),
                    hits=int(track.hits),
                    age=int(track.age),
                    is_current=int(track.time_since_update) == 0,
                )
            )
        return output


class IoUTracker(_BaseTracker):
    """Greedy IoU + motion tracker used when DeepSORT is unavailable.

    It tracks boxes with a constant-velocity prediction, matches by IoU first and
    centre distance second, and requires ``n_init`` hits before reporting a track.
    """

    degraded = True

    def __init__(
        self,
        *,
        max_age: int = 45,
        n_init: int = 3,
        iou_threshold: float = 0.25,
        center_gate: float = 0.12,
        id_offset: int = 0,
    ) -> None:
        self.max_age = max_age
        self.n_init = n_init
        self.iou_threshold = iou_threshold
        self.center_gate = center_gate
        self._next_id = 1 + id_offset
        self._tracks: dict[int, dict] = {}

    def update(self, detections: list[Detection], frame: np.ndarray) -> list[ActiveTrack]:
        frame_height, frame_width = frame.shape[:2]
        diagonal = max(1.0, float(np.hypot(frame_width, frame_height)))
        gate_px = self.center_gate * diagonal

        # Predict existing tracks forward with their last velocity.
        for state in self._tracks.values():
            vx, vy = state["velocity"]
            x1, y1, x2, y2 = state["bbox"]
            state["predicted"] = (x1 + vx, y1 + vy, x2 + vx, y2 + vy)

        unmatched_tracks = set(self._tracks)
        unmatched_detections = set(range(len(detections)))
        matches: list[tuple[int, int]] = []

        # Pass 1: IoU overlap (most reliable when the bird barely moved).
        candidates: list[tuple[float, int, int]] = []
        for track_id in unmatched_tracks:
            predicted = self._tracks[track_id]["predicted"]
            for det_index in unmatched_detections:
                overlap = bbox_iou(predicted, detections[det_index].bbox)
                if overlap >= self.iou_threshold:
                    candidates.append((overlap, track_id, det_index))
        for overlap, track_id, det_index in sorted(candidates, reverse=True):
            if track_id in unmatched_tracks and det_index in unmatched_detections:
                matches.append((track_id, det_index))
                unmatched_tracks.discard(track_id)
                unmatched_detections.discard(det_index)

        # Pass 2: nearest centre for small/fast birds whose boxes barely overlap.
        candidates = []
        for track_id in unmatched_tracks:
            predicted_center = bbox_center(self._tracks[track_id]["predicted"])
            for det_index in unmatched_detections:
                distance = euclidean(predicted_center, bbox_center(detections[det_index].bbox))
                if distance <= gate_px:
                    candidates.append((distance, track_id, det_index))
        for distance, track_id, det_index in sorted(candidates):
            if track_id in unmatched_tracks and det_index in unmatched_detections:
                matches.append((track_id, det_index))
                unmatched_tracks.discard(track_id)
                unmatched_detections.discard(det_index)

        for track_id, det_index in matches:
            state = self._tracks[track_id]
            detection = detections[det_index]
            previous_center = bbox_center(state["bbox"])
            new_center = bbox_center(detection.bbox)
            state["velocity"] = (
                0.5 * state["velocity"][0] + 0.5 * (new_center[0] - previous_center[0]),
                0.5 * state["velocity"][1] + 0.5 * (new_center[1] - previous_center[1]),
            )
            state["bbox"] = detection.bbox
            state["hits"] += 1
            state["confidence"] = detection.confidence
            state["time_since_update"] = 0

        for track_id in unmatched_tracks:
            state = self._tracks[track_id]
            state["time_since_update"] += 1
            if state["time_since_update"] > self.max_age:
                del self._tracks[track_id]

        for det_index in unmatched_detections:
            detection = detections[det_index]
            track_id = self._next_id
            self._next_id += 1
            self._tracks[track_id] = {
                "bbox": detection.bbox,
                "predicted": detection.bbox,
                "velocity": (0.0, 0.0),
                "hits": 1,
                "confidence": detection.confidence,
                "time_since_update": 0,
            }

        output: list[ActiveTrack] = []
        for track_id, state in self._tracks.items():
            if state["hits"] < self.n_init:
                continue
            output.append(
                ActiveTrack(
                    track_id=track_id,
                    bbox=state["bbox"],
                    confidence=state["confidence"],
                    hits=state["hits"],
                    age=state["hits"] + state["time_since_update"],
                    is_current=state["time_since_update"] == 0,
                )
            )
        return output


class HybridTracker(_BaseTracker):
    """Use DeepSORT when healthy, fall back to IoU tracking automatically."""

    def __init__(
        self,
        *,
        max_age: int = 45,
        n_init: int = 3,
        max_cosine_distance: float = 0.35,
        embedder: str | None = "mobilenet",
    ) -> None:
        self._fallback = IoUTracker(max_age=max_age, n_init=n_init, id_offset=900_000)
        self._primary: _BaseTracker | None = None
        try:
            self._primary = DeepSortAdapter(
                max_age=max_age,
                n_init=n_init,
                max_cosine_distance=max_cosine_distance,
                embedder=embedder,
            )
        except Exception as exc:  # noqa: BLE001 - any embedder/torch failure is non-fatal
            logger.warning("DeepSORT unavailable, using IoU fallback tracker: %s", exc)
        self._active: _BaseTracker = self._primary or self._fallback
        self.degraded = self._active is self._fallback
        self.fallback_reason = "" if self._primary else "deepsort_init_failed"

    @property
    def backend(self) -> str:
        return "deepsort" if self._active is self._primary else "iou"

    def update(self, detections: list[Detection], frame: np.ndarray) -> list[ActiveTrack]:
        if self._active is self._primary and self._primary is not None:
            try:
                return self._primary.update(detections, frame)
            except Exception as exc:  # noqa: BLE001 - degrade instead of crashing the run
                logger.exception("DeepSORT failed mid-run, switching to IoU tracker: %s", exc)
                self._active = self._fallback
                self.degraded = True
                self.fallback_reason = f"deepsort_runtime_error: {exc}"[:200]
        return self._fallback.update(detections, frame)


def build_tracker(
    *,
    max_age: int = 45,
    n_init: int = 3,
    max_cosine_distance: float = 0.35,
    embedder: str | None = "mobilenet",
) -> HybridTracker:
    return HybridTracker(
        max_age=max_age,
        n_init=n_init,
        max_cosine_distance=max_cosine_distance,
        embedder=embedder,
    )
