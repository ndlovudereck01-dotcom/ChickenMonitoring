"""Dataset review helpers: read/write YOLO labels for the in-app annotation tool.

The dataset directory is the source of truth (the same one ``train_model`` reads):

    <dataset>/images/{train,val}/<stem>.jpg
    <dataset>/labels/{train,val}/<stem>.txt
    <dataset>/review_state.json

Keeping labels as plain files means the annotator, ``build_dataset`` and
``train_model`` never disagree about the ground truth.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings

SPLITS = ("train", "val")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
STEM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
CLASS_INDEX = 0


class LabelError(ValueError):
    """Raised for invalid annotation input."""


def dataset_root() -> Path:
    return Path(settings.CHICKEN_DATASET_DIR)


def state_path(root: Path | None = None) -> Path:
    return (root or dataset_root()) / "review_state.json"


def load_state(root: Path | None = None) -> dict:
    path = state_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict, root: Path | None = None) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def image_size(path: Path) -> tuple[int, int] | None:
    from PIL import Image

    try:
        with Image.open(path) as handle:
            return int(handle.width), int(handle.height)
    except Exception:
        return None


def read_boxes(label_path: Path) -> list[list[float]]:
    """Return normalised ``[cx, cy, w, h]`` boxes, dropping anything malformed."""
    if not label_path.exists():
        return []
    boxes: list[list[float]] = []
    for line in label_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        try:
            cx, cy, width, height = (float(value) for value in parts[1:])
        except ValueError:
            continue
        if width <= 0 or height <= 0:
            continue
        boxes.append(
            [
                round(min(1.0, max(0.0, cx)), 6),
                round(min(1.0, max(0.0, cy)), 6),
                round(min(1.0, width), 6),
                round(min(1.0, height), 6),
            ]
        )
    return boxes


def write_boxes(label_path: Path, boxes: list[list[float]]) -> None:
    label_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{CLASS_INDEX} {cx:.6f} {cy:.6f} {width:.6f} {height:.6f}"
        for cx, cy, width, height in boxes
    ]
    temporary = label_path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(temporary, label_path)


def resolve_image(split: str, stem: str) -> Path:
    """Map an API request onto a file, refusing anything outside the dataset."""
    if split not in SPLITS:
        raise LabelError("Unknown split.")
    if not STEM_PATTERN.match(stem or ""):
        raise LabelError("Invalid image name.")
    root = dataset_root()
    for suffix in IMAGE_SUFFIXES:
        candidate = (root / "images" / split / f"{stem}{suffix}").resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise LabelError("Invalid image name.") from exc
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(stem)


def list_images() -> list[dict]:
    """Every dataset image with its boxes, size and review status."""
    root = dataset_root()
    state = load_state(root)
    items: list[dict] = []
    for split in SPLITS:
        image_dir = root / "images" / split
        if not image_dir.is_dir():
            continue
        for path in sorted(image_dir.iterdir()):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            stem = path.stem
            boxes = read_boxes(root / "labels" / split / f"{stem}.txt")
            size = image_size(path)
            record = state.get(f"{split}/{stem}", {})
            items.append(
                {
                    "split": split,
                    "stem": stem,
                    "boxes": boxes,
                    "width": size[0] if size else None,
                    "height": size[1] if size else None,
                    "reviewed": bool(record.get("reviewed")),
                    "updated_at": record.get("updated_at", ""),
                }
            )
    return items


def summary(items: list[dict] | None = None) -> dict:
    items = items if items is not None else list_images()
    reviewed = sum(1 for item in items if item["reviewed"])
    return {
        "images": len(items),
        "reviewed": reviewed,
        "pending": len(items) - reviewed,
        "boxes": sum(len(item["boxes"]) for item in items),
        "empty_labels": sum(1 for item in items if not item["boxes"]),
        "root": str(dataset_root()),
    }


def _clean_boxes(payload) -> list[list[float]]:
    if not isinstance(payload, list):
        raise LabelError("boxes must be a list.")
    if len(payload) > 500:
        raise LabelError("Too many boxes in one image.")
    cleaned: list[list[float]] = []
    for entry in payload:
        if not isinstance(entry, list) or len(entry) < 4:
            raise LabelError("Each box needs four numbers.")
        try:
            cx, cy, width, height = (float(value) for value in entry[:4])
        except (TypeError, ValueError) as exc:
            raise LabelError("Box coordinates must be numbers.") from exc
        if not all(0.0 <= value <= 1.0 for value in (cx, cy, width, height)):
            raise LabelError("Box coordinates must be normalised to 0-1.")
        if width < 0.002 or height < 0.002:
            raise LabelError("A box is too small to be useful.")
        # Keep the box inside the frame after rounding.
        half_w, half_h = width / 2.0, height / 2.0
        cx = min(1.0 - half_w, max(half_w, cx))
        cy = min(1.0 - half_h, max(half_h, cy))
        cleaned.append([round(cx, 6), round(cy, 6), round(width, 6), round(height, 6)])
    return cleaned


def save_annotation(split: str, stem: str, boxes, reviewed: bool = False) -> dict:
    image_path = resolve_image(split, stem)
    cleaned = _clean_boxes(boxes)
    write_boxes(dataset_root() / "labels" / split / f"{image_path.stem}.txt", cleaned)

    root = dataset_root()
    state = load_state(root)
    key = f"{split}/{image_path.stem}"
    state[key] = {
        "reviewed": bool(reviewed),
        "updated_at": datetime.now(dt_timezone.utc).isoformat(timespec="seconds"),
    }
    save_state(state, root)
    return {"boxes": cleaned, "reviewed": bool(reviewed), "key": key}
