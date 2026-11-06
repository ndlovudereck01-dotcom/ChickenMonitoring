"""YOLOv11 detector wrapper.

The detector is intentionally class-agnostic: point it at a chicken-specific
YOLO11 checkpoint for best accuracy, or let it fall back to the COCO ``bird``
class that ships with ``yolo11n.pt``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .geometry import BBox

logger = logging.getLogger("monitoring.ml.detector")

# Keywords used to auto-detect poultry classes in a custom checkpoint.
POULTRY_KEYWORDS = ("chicken", "hen", "poultry", "chick", "broiler", "layer", "cock")

_COCO_BIRD_CLASS_ID = 14

_model_cache: dict[tuple, "Detector"] = {}
_model_cache_lock = threading.Lock()


class DetectorError(RuntimeError):
    """Raised when the model cannot be loaded or inference fails."""


@dataclass(slots=True)
class Detection:
    bbox: BBox
    confidence: float
    class_id: int
    label: str


def resolve_class_ids(names: dict[int, str], configured: str = "") -> list[int] | None:
    """Work out which class ids represent chickens for this checkpoint.

    Priority: explicit configuration > poultry-named classes > COCO ``bird``.
    ``None`` means "keep every class" and is only used for unrecognised models.
    """
    if configured:
        try:
            ids = [int(part) for part in configured.replace(" ", "").split(",") if part]
        except ValueError as exc:
            raise DetectorError(f"CHICKEN_CLASS_IDS must be comma separated integers: {exc}") from exc
        return ids or None

    lowered = {int(idx): str(name).lower() for idx, name in names.items()}
    for keyword in POULTRY_KEYWORDS:
        matches = [idx for idx, name in lowered.items() if keyword in name]
        if matches:
            logger.info("Using poultry classes %s from checkpoint", matches)
            return sorted(matches)

    bird = [idx for idx, name in lowered.items() if name == "bird"]
    if bird:
        logger.warning(
            "No chicken-specific weights found: falling back to the COCO 'bird' class. "
            "Train a custom model with `python manage.py train_model` for best accuracy."
        )
        return bird

    logger.warning("No bird/chicken class in checkpoint; every class will be tracked.")
    return None


def list_model_classes(weights: str) -> dict[int, str]:
    """Read class names from a checkpoint without running inference."""
    from ultralytics import YOLO

    model = YOLO(weights)
    return dict(model.names or {})


class Detector:
    """Thin, caching wrapper around ``ultralytics.YOLO``."""

    def __init__(
        self,
        weights: str,
        *,
        device: str = "cpu",
        class_ids: str = "",
        torch_threads: int = 2,
    ) -> None:
        try:
            import torch
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise DetectorError(
                "PyTorch/Ultralytics are not installed. Run: pip install -r requirements.txt"
            ) from exc

        if torch_threads > 0:
            torch.set_num_threads(torch_threads)

        weights_path = Path(weights)
        resolved = weights
        if not weights_path.exists():
            if len(weights_path.parts) > 1:
                # A configured local checkpoint that is missing must not break runs:
                # fall back to the stock YOLO11 nano model (COCO "bird" class).
                logger.warning(
                    "Custom weights %s not found; falling back to yolo11n.pt", weights
                )
                resolved = "yolo11n.pt"
            else:
                # Ultralytics can fetch official checkpoints by name (e.g. yolo11n.pt).
                logger.info(
                    "Weights %s not found locally; ultralytics will resolve '%s'.", weights, weights
                )
        self.requested_weights = resolved
        try:
            self._model = YOLO(str(weights_path) if weights_path.exists() else resolved)
        except Exception as exc:
            raise DetectorError(f"Unable to load YOLO weights '{resolved}': {exc}") from exc

        self.names: dict[int, str] = dict(self._model.names or {})
        self.class_ids = resolve_class_ids(self.names, class_ids)
        self.device = device
        self._lock = threading.Lock()
        logger.info(
            "Loaded detector weights=%s device=%s classes=%s",
            self.describe,
            device,
            self.class_ids if self.class_ids is not None else "all",
        )

    @property
    def describe(self) -> str:
        return Path(self.requested_weights).name or self.requested_weights

    def detect(
        self,
        frame: np.ndarray,
        *,
        confidence: float = 0.35,
        iou: float = 0.5,
        imgsz: int = 640,
        max_detections: int = 300,
    ) -> list[Detection]:
        """Run inference on a BGR frame and return pixel-space detections."""
        height, width = frame.shape[:2]
        # YOLO requires multiples of 32; clamp to something sane for CPU inference.
        size = max(256, min(1280, int(round(imgsz / 32.0)) * 32))
        try:
            with self._lock:
                results = self._model.predict(
                    source=frame,
                    conf=float(confidence),
                    iou=float(iou),
                    imgsz=size,
                    classes=self.class_ids,
                    device=self.device,
                    verbose=False,
                    max_det=max_detections,
                )
        except Exception as exc:
            raise DetectorError(f"YOLO inference failed: {exc}") from exc

        detections: list[Detection] = []
        if not results:
            return detections
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return detections

        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        classes = boxes.cls.cpu().numpy().astype(int)
        for coords, score, class_id in zip(xyxy, confs, classes):
            x1, y1, x2, y2 = (float(v) for v in coords)
            x1 = max(0.0, min(x1, width - 1))
            x2 = max(0.0, min(x2, width - 1))
            y1 = max(0.0, min(y1, height - 1))
            y2 = max(0.0, min(y2, height - 1))
            if x2 <= x1 or y2 <= y1:
                continue
            detections.append(
                Detection(
                    bbox=(x1, y1, x2, y2),
                    confidence=float(score),
                    class_id=int(class_id),
                    label=str(self.names.get(int(class_id), class_id)),
                )
            )
        return detections


def get_detector(
    weights: str,
    *,
    device: str = "cpu",
    class_ids: str = "",
    torch_threads: int = 2,
) -> Detector:
    """Process-wide detector cache: loading weights is expensive, inference is not."""
    key = (str(weights), device, class_ids, torch_threads)
    with _model_cache_lock:
        detector = _model_cache.get(key)
        if detector is None:
            detector = Detector(
                weights, device=device, class_ids=class_ids, torch_threads=torch_threads
            )
            _model_cache[key] = detector
        return detector
