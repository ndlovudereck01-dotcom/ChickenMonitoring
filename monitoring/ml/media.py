"""Video probing, transcoding and frame I/O helpers."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger("monitoring.ml.media")

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}


class MediaError(RuntimeError):
    """Raised when a video cannot be read or written."""


@dataclass(frozen=True)
class VideoProbe:
    width: int
    height: int
    fps: float
    duration_seconds: float
    frame_count: int

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


def probe_video(path: str | Path) -> VideoProbe:
    """Validate that ``path`` is a decodable video and return its metadata."""
    path = Path(path)
    if not path.exists():
        raise MediaError("The uploaded file could not be found on disk.")
    if path.stat().st_size == 0:
        raise MediaError("The uploaded file is empty.")

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise MediaError("This file is not a readable video. Use MP4, MOV, AVI, MKV or WebM.")
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        ok, frame = capture.read()
        if not ok or frame is None or frame.size == 0:
            raise MediaError("The video contains no decodable frames.")
        height, width = frame.shape[:2]

        if not 0 < fps < 240:
            fps = 25.0
        duration = frame_count / fps if frame_count > 0 else 0.0
        if duration <= 0 and frame_count <= 0:
            raise MediaError("The video has no frames to process.")
        if width < 64 or height < 64:
            raise MediaError("The video resolution is too small to analyse.")
    finally:
        capture.release()

    return VideoProbe(
        width=width,
        height=height,
        fps=round(fps, 3),
        duration_seconds=round(duration, 2),
        frame_count=frame_count,
    )


def open_capture(source: str | int) -> cv2.VideoCapture:
    """Open a file or RTSP/HTTP stream with a low-latency buffer."""
    if isinstance(source, str) and source.lower().startswith(("rtsp://", "http://", "https://", "rtmp://")):
        capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    else:
        capture = cv2.VideoCapture(source)
    try:
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:  # not all backends support it
        pass
    return capture


def write_jpeg_atomic(path: str | Path, frame: np.ndarray, quality: int = 72) -> bool:
    """Write a JPEG atomically so readers never see a half-written frame."""
    path = Path(path)
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return False
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(buffer.tobytes())
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("Cannot write live frame %s: %s", path, exc)
        tmp.unlink(missing_ok=True)
        return False


def read_jpeg(path: str | Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def write_thumbnail(frame: np.ndarray, path: str | Path, max_width: int = 960) -> bool:
    height, width = frame.shape[:2]
    if width > max_width:
        ratio = max_width / float(width)
        frame = cv2.resize(frame, (max_width, int(height * ratio)), interpolation=cv2.INTER_AREA)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return bool(cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85]))


def transcode_h264(source: str | Path, destination: str | Path) -> bool:
    """Re-encode to browser-friendly H.264. Returns False when ffmpeg is unavailable."""
    source, destination = Path(source), Path(destination)
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning("ffmpeg not available, keeping raw output: %s", exc)
        return False

    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=3600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Transcode failed (%s); keeping raw output.", exc)
        return False
    if result.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
        logger.warning(
            "Transcode returned %s: %s",
            result.returncode,
            result.stderr.decode("utf-8", "ignore")[:300],
        )
        destination.unlink(missing_ok=True)
        return False
    return True


def move_into_place(source: str | Path, destination: str | Path) -> None:
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def file_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
