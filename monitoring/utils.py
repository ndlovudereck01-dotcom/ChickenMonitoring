"""Shared helpers for views, the pipeline and reports."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from .models import AuditLog

logger = logging.getLogger("monitoring")

AUDIT_ACTION_MAX = 64


def audit(request, action: str, *, target=None, detail: str = "") -> AuditLog:
    """Record a security-relevant action. Never raises into the caller."""
    from .security import get_client_ip

    user = getattr(request, "user", None)
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    target_type = target.__class__.__name__ if target is not None else ""
    target_id = str(getattr(target, "pk", "") or "") if target is not None else ""
    try:
        return AuditLog.objects.create(
            user=user,
            action=action[:AUDIT_ACTION_MAX],
            target_type=target_type[:64],
            target_id=target_id[:64],
            detail=detail[:300],
            ip_address=get_client_ip(request),
            user_agent=(request.META.get("HTTP_USER_AGENT", "") or "")[:255],
        )
    except Exception:  # pragma: no cover - auditing must never break a request
        logger.exception("Failed to write audit entry for action=%s", action)
        return AuditLog()


def humanize_seconds(seconds: float | int | None) -> str:
    total = int(seconds or 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def downsample(points: list[dict], max_points: int = 240) -> list[dict]:
    """Reduce a time series for chart rendering while keeping the shape."""
    if len(points) <= max_points:
        return points
    step = len(points) / float(max_points)
    return [points[min(len(points) - 1, int(i * step))] for i in range(max_points)]


def day_bounds(days: int = 7):
    """Return (start, end) datetimes for the last ``days`` complete days."""
    end = timezone.now()
    start = end - timedelta(days=days)
    return start, end
