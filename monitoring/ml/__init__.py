"""Computer-vision pipeline: YOLO11 detection + DeepSORT tracking + counting."""

from .counting import CountingEngine, CountingEvent, Snapshot  # noqa: F401
from .detector import Detector, DetectorError, resolve_class_ids  # noqa: F401
from .tracker import ActiveTrack, build_tracker  # noqa: F401
