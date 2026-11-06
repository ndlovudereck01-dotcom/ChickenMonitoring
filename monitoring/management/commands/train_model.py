"""Fine-tune a YOLOv11 model on a chicken dataset and install it as the default."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


class Command(BaseCommand):
    help = (
        "Train YOLOv11 on a chicken dataset (see datasets/chickens.yaml) and install the "
        "best checkpoint as models/chicken_yolo11.pt."
    )

    def add_arguments(self, parser):
        parser.add_argument("--data", default="datasets/chickens.yaml", help="Dataset YAML.")
        parser.add_argument("--model", default="yolo11n.pt", help="Base YOLOv11 checkpoint.")
        parser.add_argument("--epochs", type=int, default=100)
        parser.add_argument("--imgsz", type=int, default=640)
        parser.add_argument("--batch", type=int, default=16)
        parser.add_argument("--device", default="cpu", help="cpu, 0, 0,1 …")
        parser.add_argument("--workers", type=int, default=2)
        parser.add_argument("--name", default="chicken_yolo11", help="Run name.")
        parser.add_argument(
            "--install",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="Copy best.pt to models/chicken_yolo11.pt (default: yes; use --no-install to keep the current model).",
        )

    def _precheck(self, data_path: Path) -> None:
        """Fail early on an empty/unusable dataset instead of inside ultralytics."""
        try:
            import yaml

            config = yaml.safe_load(data_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise CommandError(f"Cannot read dataset config {data_path}: {exc}") from exc

        root = Path(config.get("path") or data_path.parent)
        if not root.is_absolute():
            root = (data_path.parent / root).resolve()

        counts: dict[str, int] = {}
        for split in ("train", "val"):
            entry = config.get(split)
            target = None
            if isinstance(entry, str):
                target = Path(entry)
                if not target.is_absolute():
                    target = root / target
            if target is None or not target.is_dir():
                counts[split] = 0
                continue
            counts[split] = sum(
                1 for item in target.iterdir() if item.suffix.lower() in IMAGE_SUFFIXES
            )

        self.stdout.write(
            f"Dataset {data_path}: train={counts['train']} val={counts['val']} images"
        )
        if counts["train"] == 0:
            raise CommandError(
                f"No training images under {root}. Build a dataset from footage first:\n"
                "  python manage.py build_dataset --source <footage-dir-or-video>"
            )
        if counts["val"] == 0:
            raise CommandError(
                f"No validation images under {root}. Re-run build_dataset without --val-ratio 0 "
                "so the model has frames it never trained on."
            )
        if counts["train"] < 50:
            self.stdout.write(
                self.style.WARNING(
                    "Fewer than 50 training images: expect a noisy mAP. Add more footage or "
                    "lower --interval when building the dataset."
                )
            )

    def handle(self, *args, **options):
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise CommandError("ultralytics is not installed: pip install -r requirements.txt") from exc

        data_path = Path(options["data"])
        if not data_path.exists():
            raise CommandError(
                f"Dataset config not found: {data_path}. "
                "Start from datasets/chickens.yaml and point it at your images."
            )

        self._precheck(data_path)

        self.stdout.write(
            f"Training {options['model']} on {data_path} "
            f"({options['epochs']} epochs, imgsz={options['imgsz']}, device={options['device']})…"
        )
        model = YOLO(options["model"])
        results = model.train(
            data=str(data_path),
            epochs=options["epochs"],
            imgsz=options["imgsz"],
            batch=options["batch"],
            device=options["device"],
            workers=options["workers"],
            project=str(Path(settings.BASE_DIR) / "runs" / "train"),
            name=options["name"],
            exist_ok=True,
            patience=25,
            seed=0,
            plots=True,
        )

        best = Path(getattr(results, "save_dir", "")) / "weights" / "best.pt"
        if not best.exists():
            raise CommandError(f"Training finished but best weights were not found at {best}.")

        self.stdout.write(self.style.SUCCESS(f"Best checkpoint: {best}"))
        metrics = getattr(results, "results_dict", None)
        if metrics:
            for key in ("metrics/mAP50(B)", "metrics/mAP50-95(B)", "metrics/precision(B)", "metrics/recall(B)"):
                if key in metrics:
                    self.stdout.write(f"  {key}: {metrics[key]:.4f}")

        if options["install"]:
            destination = Path(settings.MODEL_DIR) / "chicken_yolo11.pt"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(best, destination)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Installed as {destination}. Restart the worker so it loads the new weights."
                )
            )
