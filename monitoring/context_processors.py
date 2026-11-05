"""Sidebar badges and global template context."""

from __future__ import annotations

from django.db.models import Count, Q

from .models import Alert, Camera, DetectionRun


def navigation(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    cameras = Camera.objects.aggregate(
        total=Count("id"),
        online=Count("id", filter=Q(status=Camera.Status.ONLINE)),
    )
    active_runs = DetectionRun.objects.filter(
        status__in=[DetectionRun.Status.QUEUED, DetectionRun.Status.RUNNING]
    ).count()
    open_alerts = Alert.objects.filter(is_acknowledged=False).count()

    return {
        "nav_camera_total": cameras["total"] or 0,
        "nav_camera_online": cameras["online"] or 0,
        "nav_active_runs": active_runs,
        "nav_open_alerts": open_alerts,
    }
