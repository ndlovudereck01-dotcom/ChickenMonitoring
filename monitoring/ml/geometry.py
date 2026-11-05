"""Geometry helpers shared by the tracker, counter and renderer.

All polygons/lines arriving from the UI are normalised to the 0-1 range so the
same configuration works for any camera resolution. This module converts them to
pixel space for a concrete frame.
"""

from __future__ import annotations

from typing import Iterable, Sequence

Point = tuple[float, float]
BBox = tuple[float, float, float, float]


def to_pixels(points: Iterable[Sequence[float]], width: int, height: int) -> list[Point]:
    return [(float(x) * width, float(y) * height) for x, y in points]


def bbox_center(bbox: BBox) -> Point:
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def bbox_foot(bbox: BBox) -> Point:
    """Ground-contact point of a bird (bottom-centre of its box)."""
    x1, _, x2, y2 = bbox
    return ((x1 + x2) / 2.0, float(y2))


def bbox_area(bbox: BBox) -> float:
    x1, y1, x2, y2 = bbox
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def bbox_iou(a: BBox, b: BBox) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = inter_w * inter_h
    union = bbox_area(a) + bbox_area(b) - intersection
    return intersection / union if union > 0 else 0.0


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray casting test (works for convex and concave polygons)."""
    x, y = point
    inside = False
    count = len(polygon)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Only count edges that straddle the horizontal ray to the right.
        if (yi > y) != (yj > y):
            denominator = yj - yi
            if denominator == 0:
                continue
            x_intersect = (xj - xi) * (y - yi) / denominator + xi
            if x < x_intersect:
                inside = not inside
        j = i
    return inside


def line_side(point: Point, line: Sequence[Point]) -> float:
    """Signed distance-proportional side of ``point`` relative to a segment.

    Positive on one side, negative on the other, ~0 on the line. Only the sign
    matters for crossing detection.
    """
    (x1, y1), (x2, y2) = line
    cross = (x2 - x1) * (point[1] - y1) - (y2 - y1) * (point[0] - x1)
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return cross / length if length else 0.0


def line_projection(point: Point, line: Sequence[Point]) -> float:
    """Position of ``point`` projected on the segment, 0 at p1 and 1 at p2."""
    (x1, y1), (x2, y2) = line
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return 0.0
    return ((point[0] - x1) * dx + (point[1] - y1) * dy) / length_sq


def euclidean(a: Point, b: Point) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
