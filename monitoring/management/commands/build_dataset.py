"""Turn raw footage into a YOLO training set: frames + labels + dataset YAML.

Two labelling modes:

* default - auto-label every extracted frame with the detector currently
  configured in ``CHICKEN_MODEL_PATH`` (pseudo-labelling). Fast, and good enough
  to bootstrap, but it can only be as accurate as the teacher model, so review
  the output with ``--review`` and correct boxes in LabelImg/CVAT when you can.
* ``--labels`` - copy your own YOLO ``.txt`` labels (same stem as the image)
  instead of running the teacher.
"""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import cv2

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from monitoring.ml.detector import Detector, DetectorError, get_detector
from monitoring.ml.media import ALLOWED_VIDEO_EXTENSIONS, MediaError, open_capture, probe_video

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
CLASS_INDEX = 0  # chickens.yaml has a single class: chicken


def _safe_stem(name: str) -> str:
    """Filesystem/label-friendly stem: no spaces or punctuation surprises."""
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("_")
    return cleaned or "frame"


class Command(BaseCommand):
    help = (
        "Extract frames from footage, label them (auto or from --labels) and write a "
        "YOLO dataset ready for `manage.py train_model`."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            action="append",
            required=True,
            metavar="PATH",
            help="Video or image file, or a directory (searched recursively). Repeatable.",
        )
        parser.add_argument(
            "--out", default="datasets/chickens", help="Dataset root (default: %(default)s)."
        )
        parser.add_argument(
            "--yaml",
            default=str(Path("datasets") / "chickens.yaml"),
            help="Dataset YAML to write (default: %(default)s).",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=1.0,
            metavar="SECONDS",
            help="Sample one frame every N seconds of video (default: %(default)s).",
        )
        parser.add_argument(
            "--max-per-video",
            type=int,
            default=300,
            help="Cap frames taken from a single video (default: %(default)s).",
        )
        parser.add_argument(
            "--conf", type=float, default=min(0.45, max(0.25, settings.CHICKEN_CONFIDENCE))
        )
        parser.add_argument("--imgsz", type=int, default=640)
        parser.add_argument(
            "--val-ratio",
            type=float,
            default=0.15,
            help="Fraction of frames per source held out for validation (default: %(default)s).",
        )
        parser.add_argument(
            "--min-area",
            type=float,
            default=0.0005,
            help="Drop boxes smaller than this fraction of the frame (default: %(default)s).",
        )
        parser.add_argument(
            "--max-area",
            type=float,
            default=0.85,
            help="Drop boxes larger than this fraction of the frame (default: %(default)s).",
        )
        parser.add_argument(
            "--keep-empty",
            action="store_true",
            help="Keep frames with no detections as background (negative) samples.",
        )
        parser.add_argument(
            "--labels",
            metavar="DIR",
            help="Use existing YOLO labels from DIR instead of auto-labelling frames.",
        )
        parser.add_argument(
            "--review",
            metavar="DIR",
            help="Write annotated copies of the labelled frames here for visual checking.",
        )
        parser.add_argument("--jpeg-quality", type=int, default=92)

    # ------------------------------------------------------------------ helpers
    def _collect(self, sources: list[str]) -> list[Path]:
        found: list[Path] = []
        for raw in sources:
            path = Path(raw).expanduser()
            if not path.exists():
                raise CommandError(f"Source not found: {path}")
            if path.is_file():
                found.append(path)
                continue
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in (
                    ALLOWED_VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
                ):
                    found.append(child)
        if not found:
            raise CommandError(
                "No video or image files found. Pass a directory containing footage "
                "(MP4, MOV, AVI, MKV, WebM) or individual images."
            )
        return found

    def _extract_video(
        self, video: Path, staging: Path, interval: float, cap_frames: int, quality: int
    ) -> list[Path]:
        """Write sampled frames from one video and return the files created."""
        probe = probe_video(video)
        step = max(1, int(round(probe.fps * max(interval, 0.05))))
        capture = open_capture(str(video))
        if not capture.isOpened():
            raise MediaError(f"Cannot open video: {video}")
        written: list[Path] = []
        scanned = 0
        stem = _safe_stem(video.stem)
        try:
            while len(written) < cap_frames:
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                if scanned % step == 0 and frame.size:
                    target = staging / f"{stem}_{scanned:06d}.jpg"
                    if cv2.imwrite(str(target), frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality]):
                        written.append(target)
                scanned += 1
        finally:
            capture.release()
        self.stdout.write(
            f"  {video.name}: {probe.resolution} @ {probe.fps:g} fps, "
            f"{len(written)} frame(s) from {scanned} scanned"
        )
        if not written:
            self.stdout.write(self.style.WARNING(f"    no frames read from {video.name}"))
        return written

    def _yolo_lines(
        self, detections, width: int, height: int, min_area: float, max_area: float
    ) -> list[str]:
        lines: list[str] = []
        for detection in detections:
            x1, y1, x2, y2 = detection.bbox
            cx = ((x1 + x2) / 2.0) / width
            cy = ((y1 + y2) / 2.0) / height
            bw = (x2 - x1) / width
            bh = (y2 - y1) / height
            area = bw * bh
            if area < min_area or area > max_area:
                continue
            lines.append(
                f"{CLASS_INDEX} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"
            )
        return lines

    def _review_image(
        self, image_path: Path, detections, destination: Path
    ) -> None:
        frame = cv2.imread(str(image_path))
        if frame is None:
            return
        for detection in detections:
            x1, y1, x2, y2 = (int(v) for v in detection.bbox)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 255), 2, cv2.LINE_AA)
            cv2.putText(
                frame,
                f"{detection.confidence:.2f}",
                (x1 + 2, max(12, y1 - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 200, 255),
                1,
                cv2.LINE_AA,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(destination), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])

    # -------------------------------------------------------------------- handle
    def handle(self, *args, **options):
        out_root = Path(options["out"]).resolve()
        yaml_path = Path(options["yaml"]).resolve()
        sources = self._collect(options["source"])
        videos = [p for p in sources if p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS]
        images = [p for p in sources if p.suffix.lower() in IMAGE_EXTENSIONS]
        if not videos and not images:
            raise CommandError("Nothing usable in the given sources.")

        staging = out_root / "_staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        detector: Detector | None = None
        labels_dir: Path | None = None
        if options["labels"]:
            labels_dir = Path(options["labels"]).expanduser().resolve()
            if not labels_dir.is_dir():
                raise CommandError(f"--labels directory not found: {labels_dir}")
            self.stdout.write(f"Using existing labels from {labels_dir}")
        else:
            try:
                detector = get_detector(
                    settings.CHICKEN_MODEL_PATH,
                    device=settings.CHICKEN_DEVICE,
                    class_ids=settings.CHICKEN_CLASS_IDS,
                )
            except DetectorError as exc:
                raise CommandError(f"Cannot load the detector for auto-labelling: {exc}") from exc
            self.stdout.write(
                f"Auto-labelling with {detector.describe} (conf={options['conf']}, "
                f"classes={detector.class_ids if detector.class_ids is not None else 'all'})"
            )

        # 1. Frame extraction / image intake.
        extracted: list[tuple[Path, str]] = []  # (staged image, source key)
        self.stdout.write(self.style.MIGRATE_HEADING("Frames"))
        for video in videos:
            for image in self._extract_video(
                video,
                staging,
                options["interval"],
                options["max_per_video"],
                options["jpeg_quality"],
            ):
                extracted.append((image, _safe_stem(video.stem)))

        for image in images:
            group = _safe_stem(image.parent.name or "images")
            target = staging / f"{group}_{_safe_stem(image.stem)}.jpg"
            suffix = 1
            while target.exists():
                target = staging / f"{group}_{_safe_stem(image.stem)}_{suffix}.jpg"
                suffix += 1
            frame = cv2.imread(str(image))
            if frame is None:
                self.stdout.write(self.style.WARNING(f"  skipped unreadable image: {image}"))
                continue
            cv2.imwrite(
                str(target), frame, [int(cv2.IMWRITE_JPEG_QUALITY), options["jpeg_quality"]]
            )
            extracted.append((target, group))

        if not extracted:
            raise CommandError("No frames could be extracted from the given sources.")

        # 2. Labelling.
        self.stdout.write(self.style.MIGRATE_HEADING("Labels"))
        labelled: list[tuple[Path, list[str], str]] = []
        empty = dropped_area = 0
        review_root = Path(options["review"]).expanduser().resolve() if options["review"] else None

        for index, (image_path, source_key) in enumerate(extracted, start=1):
            frame = cv2.imread(str(image_path))
            if frame is None:
                continue
            height, width = frame.shape[:2]

            if labels_dir is not None:
                origin_label = labels_dir / f"{image_path.stem}.txt"
                lines = []
                if origin_label.exists():
                    for line in origin_label.read_text(encoding="utf-8").splitlines():
                        parts = line.split()
                        if len(parts) != 5:
                            continue
                        try:
                            values = [float(v) for v in parts[1:]]
                        except ValueError:
                            continue
                        if not (options["min_area"] <= values[2] * values[3] <= options["max_area"]):
                            dropped_area += 1
                            continue
                        lines.append(f"{CLASS_INDEX} " + " ".join(f"{v:.6f}" for v in values))
                detections = []
            else:
                assert detector is not None
                try:
                    detections = detector.detect(
                        frame, confidence=options["conf"], imgsz=options["imgsz"]
                    )
                except DetectorError as exc:
                    raise CommandError(f"Inference failed on {image_path.name}: {exc}") from exc
                lines = self._yolo_lines(
                    detections, width, height, options["min_area"], options["max_area"]
                )
                dropped_area += len(detections) - len(lines)

            if not lines and not options["keep_empty"]:
                empty += 1
                image_path.unlink(missing_ok=True)
                continue

            if review_root is not None:
                self._review_image(image_path, detections, review_root / image_path.name)
            labelled.append((image_path, lines, source_key))

            if index % 50 == 0:
                self.stdout.write(f"  labelled {index}/{len(extracted)} frame(s)…")

        if not labelled:
            raise CommandError(
                "Every frame was dropped: no detections above --conf and --keep-empty was not set. "
                "Lower --conf or point --source at footage with visible birds."
            )

        # 3. Split per source so validation frames are not near-duplicates of training frames.
        stride = max(2, int(round(1.0 / options["val_ratio"]))) if options["val_ratio"] > 0 else 0
        groups: dict[str, list[tuple[Path, list[str], str]]] = defaultdict(list)
        for item in labelled:
            groups[item[2]].append(item)

        split_assignment: dict[Path, str] = {}
        for key, items in groups.items():
            items.sort(key=lambda item: item[0].name)
            val_taken = 0
            for position, (image_path, _lines, _key) in enumerate(items):
                is_val = bool(stride) and position % stride == 0
                split_assignment[image_path] = "val" if is_val else "train"
                val_taken += int(is_val)
            if stride and items and not val_taken:
                split_assignment[items[-1][0]] = "val"
        if stride and all(split == "train" for split in split_assignment.values()):
            split_assignment[sorted(split_assignment)[-1]] = "val"

        # 4. Write the dataset tree.
        for split in ("train", "val"):
            for folder in ("images", "labels"):
                target = out_root / folder / split
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(parents=True, exist_ok=True)

        counts = {"train": 0, "val": 0}
        boxes = 0
        for image_path, lines, _key in labelled:
            split = split_assignment[image_path]
            shutil.move(str(image_path), str(out_root / "images" / split / image_path.name))
            label_path = out_root / "labels" / split / f"{image_path.stem}.txt"
            label_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            counts[split] += 1
            boxes += len(lines)

        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(
            "# Generated by `python manage.py build_dataset` on "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            "# Absolute paths so `train_model --data` works from any directory.\n"
            f"path: {out_root.as_posix()}\n"
            "train: images/train\n"
            "val: images/val\n"
            "\n"
            "names:\n"
            "  0: chicken\n",
            encoding="utf-8",
        )

        shutil.rmtree(staging, ignore_errors=True)

        self.stdout.write(self.style.MIGRATE_HEADING("Result"))
        self.stdout.write(f"  dataset   {out_root}")
        self.stdout.write(f"  yaml      {yaml_path}")
        self.stdout.write(
            f"  images    train={counts['train']} val={counts['val']}  boxes={boxes}"
        )
        if empty:
            self.stdout.write(f"  dropped   {empty} frame(s) with no birds")
        if dropped_area:
            self.stdout.write(f"  dropped   {dropped_area} box(es) outside --min-area/--max-area")
        if review_root is not None:
            self.stdout.write(f"  review    {review_root} (annotated copies of the labels)")
        if detector is not None:
            self.stdout.write(
                self.style.WARNING(
                    f"  Labels came from {detector.describe}, so they inherit its mistakes. "
                    "Check the review images and fix boxes in LabelImg/CVAT before trusting the score."
                )
            )
        if counts["val"] < 5:
            self.stdout.write(
                self.style.WARNING("  Fewer than 5 validation images: the mAP will be very noisy.")
            )
        self.stdout.write("")
        self.stdout.write("Next:")
        self.stdout.write(
            "  python manage.py train_model --data "
            f"{yaml_path} --epochs 40 --imgsz 512 --batch 8"
        )
