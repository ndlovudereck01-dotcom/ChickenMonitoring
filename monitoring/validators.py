"""Validators for the normalised geometry stored on cameras and zones."""

from __future__ import annotations

from django.core.exceptions import ValidationError


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_points(value, *, min_points: int, max_points: int, label: str) -> None:
    if value in (None, "", []):
        return
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{label} must be a list of points.")
    if not min_points <= len(value) <= max_points:
        raise ValidationError(f"{label} needs between {min_points} and {max_points} points.")
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValidationError(f"Each {label} point must be an [x, y] pair.")
        for coordinate in point:
            if not _is_number(coordinate) or not 0.0 <= float(coordinate) <= 1.0:
                raise ValidationError(
                    f"{label} coordinates are normalised: every value must be between 0 and 1."
                )


def validate_polygon(value) -> None:
    _validate_points(value, min_points=3, max_points=64, label="polygon")


def validate_line(value) -> None:
    _validate_points(value, min_points=2, max_points=2, label="counting line")
    if value:
        (x1, y1), (x2, y2) = value
        if abs(float(x1) - float(x2)) < 1e-4 and abs(float(y1) - float(y2)) < 1e-4:
            raise ValidationError("The two counting-line points must be different.")
