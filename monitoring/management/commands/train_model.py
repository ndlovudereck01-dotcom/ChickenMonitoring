"""Fine-tune a YOLOv11 model on a chicken dataset and install it as the default."""

from __future__ import annotations

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


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
            "--install/--no-install",
            dest="install",
            default=True,
            help="Copy best.pt to models/chicken_yolo11.pt (default: yes).",
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
