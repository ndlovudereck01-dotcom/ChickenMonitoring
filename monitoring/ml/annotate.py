"""Frame annotation: bounding boxes, IDs, ROI, counting line and HUD."""

from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np

from .counting import Snapshot

# High-contrast palette; index chosen from the stable root id so a bird keeps its colour.
PALETTE = [
    (0, 200, 255),
    (0, 255, 127),
    (255, 160, 0),
    (255, 105, 180),
    (147, 112, 219),
    (0, 255, 255),
    (124, 252, 0),
    (255, 215, 0),
    (100, 149, 237),
    (255, 99, 71),
]

HUD_BG = (24, 28, 27)
HUD_TEXT = (240, 244, 240)
ACCENT = (0, 200, 255)


def color_for(root_id: int) -> tuple[int, int, int]:
    return PALETTE[root_id % len(PALETTE)]


def _scale(width: int) -> float:
    return max(0.45, min(1.6, width / 1280.0))


def _label(frame, text: str, origin: tuple[int, int], color, scale: float, thickness: int = 1):
    x, y = origin
    (text_w, text_h), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    top = max(0, y - text_h - baseline - 4)
    cv2.rectangle(frame, (x, top), (x + text_w + 4, top + text_h + baseline + 4), color, -1)
    cv2.putText(
        frame,
        text,
        (x + 2, top + text_h + 2),
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (16, 20, 18),
        thickness,
        cv2.LINE_AA,
    )


def draw_roi(frame, roi: Sequence[Sequence[float]] | None):
    if not roi:
        return
    height, width = frame.shape[:2]
    points = np.array([[int(x * width), int(y * height)] for x, y in roi], dtype=np.int32)
    overlay = frame.copy()
    cv2.fillPoly(overlay, [points], (0, 200, 120))
    cv2.addWeighted(overlay, 0.12, frame, 0.88, 0, frame)
    cv2.polylines(frame, [points], True, (0, 220, 140), 2, cv2.LINE_AA)


def draw_line(frame, line: Sequence[Sequence[float]] | None, invert: bool = False):
    if not line:
        return
    height, width = frame.shape[:2]
    (x1, y1), (x2, y2) = line
    p1 = (int(x1 * width), int(y1 * height))
    p2 = (int(x2 * width), int(y2 * height))
    cv2.line(frame, p1, p2, ACCENT, 2, cv2.LINE_AA)
    cv2.circle(frame, p1, 5, ACCENT, -1, cv2.LINE_AA)
    cv2.circle(frame, p2, 5, ACCENT, -1, cv2.LINE_AA)
    in_label, out_label = ("OUT", "IN") if invert else ("IN", "OUT")
    mid_x, mid_y = (p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2
    cv2.putText(
        frame,
        f"{in_label} | {out_label}",
        (mid_x - 34, mid_y - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        ACCENT,
        1,
        cv2.LINE_AA,
    )


def draw_hud(frame, snapshot: Snapshot, *, fps: float | None = None, source_label: str = ""):
    height, width = frame.shape[:2]
    scale = _scale(width)
    lines = [
        f"Birds now: {snapshot.occupancy}",
        f"Unique: {snapshot.unique_total}",
        f"IN: {snapshot.line_in}   OUT: {snapshot.line_out}",
    ]
    if fps:
        lines.append(f"{fps:0.1f} FPS")
    if source_label:
        lines.append(source_label)

    font = cv2.FONT_HERSHEY_SIMPLEX
    thickness = max(1, int(round(scale * 1.2)))
    pad = int(10 * scale)
    text_height = int(22 * scale)
    box_w = int(250 * scale)
    box_h = pad * 2 + text_height * len(lines)
    overlay = frame.copy()
    cv2.rectangle(overlay, (pad, pad), (pad + box_w, pad + box_h), HUD_BG, -1)
    cv2.addWeighted(overlay, 0.72, frame, 0.28, 0, frame)
    cv2.rectangle(frame, (pad, pad), (pad + box_w, pad + box_h), ACCENT, 1, cv2.LINE_AA)

    y = pad + text_height
    for index, text in enumerate(lines):
        colour = ACCENT if index == 0 else HUD_TEXT
        cv2.putText(
            frame, text, (pad + 10, y), font, scale, colour, thickness, cv2.LINE_AA
        )
        y += text_height


def annotate_frame(
    frame: np.ndarray,
    snapshot: Snapshot,
    *,
    roi: Sequence[Sequence[float]] | None = None,
    line: Sequence[Sequence[float]] | None = None,
    invert_line: bool = False,
    fps: float | None = None,
    source_label: str = "",
) -> np.ndarray:
    """Draw everything on a copy of ``frame`` (BGR)."""
    height, width = frame.shape[:2]
    canvas = frame
    draw_roi(canvas, roi)
    draw_line(canvas, line, invert_line)

    scale = _scale(width)
    thickness = 2 if width >= 1000 else 1
    for state in snapshot.tracks:
        x1, y1, x2, y2 = (int(v) for v in state.bbox)
        colour = color_for(state.root_id)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), colour, thickness, cv2.LINE_AA)
        label = f"#{state.root_id} {state.confidence:.2f}"
        _label(canvas, label, (x1, max(y1 - 2, 12)), colour, max(0.4, scale * 0.62))

    draw_hud(canvas, snapshot, fps=fps, source_label=source_label)
    return canvas
