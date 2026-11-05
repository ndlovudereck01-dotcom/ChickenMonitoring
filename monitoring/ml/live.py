"""Live frame access for the browser.

The worker writes the latest annotated JPEG to a private path; web requests read
it. This keeps the streaming endpoints independent from the worker process, so a
restart of either side never breaks the other.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("monitoring.ml.live")

BOUNDARY = "frame"


def live_frame_path(camera_id: int) -> Path:
    return Path(settings.LIVE_FRAME_ROOT) / f"{camera_id}.jpg"


def read_live_frame(camera_id: int) -> bytes | None:
    try:
        return live_frame_path(camera_id).read_bytes()
    except OSError:
        return None


def live_frame_age(camera_id: int) -> float | None:
    try:
        return max(0.0, time.time() - live_frame_path(camera_id).stat().st_mtime)
    except OSError:
        return None


def mjpeg_stream(
    camera_id: int,
    *,
    max_seconds: float = 600.0,
    idle_timeout: float = 12.0,
    poll_interval: float = 0.04,
):
    """Yield a multipart MJPEG stream of the newest annotated frames."""
    started = time.monotonic()
    last_signature: float | None = None
    last_new_frame = time.monotonic()
    while time.monotonic() - started < max_seconds:
        path = live_frame_path(camera_id)
        try:
            stat = path.stat()
        except OSError:
            yield b""  # keep the connection alive while the worker connects
            time.sleep(0.25)
            if time.monotonic() - last_new_frame > idle_timeout:
                return
            continue

        if last_signature != stat.st_mtime_ns:
            try:
                payload = path.read_bytes()
            except OSError:
                payload = b""
            if payload:
                last_signature = stat.st_mtime_ns
                last_new_frame = time.monotonic()
                yield (
                    f"--{BOUNDARY}\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(payload)}\r\n\r\n"
                ).encode("ascii") + payload + b"\r\n"
        elif time.monotonic() - last_new_frame > idle_timeout:
            logger.debug("Live stream for camera %s idle, closing", camera_id)
            return
        time.sleep(poll_interval)
