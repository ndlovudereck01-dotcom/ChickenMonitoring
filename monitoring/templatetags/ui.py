"""Small presentation helpers for templates."""

from __future__ import annotations

from django import template

register = template.Library()

STATUS_CLASSES = {
    "online": "is-online",
    "running": "is-running",
    "starting": "is-starting",
    "queued": "is-queued",
    "completed": "is-completed",
    "failed": "is-failed",
    "error": "is-error",
    "cancelled": "is-cancelled",
    "offline": "is-offline",
    "idle": "is-idle",
    "critical": "is-critical",
    "warning": "is-warning",
    "info": "is-info",
}


@register.filter
def status_class(value) -> str:
    return STATUS_CLASSES.get(str(value or "").lower(), "is-idle")


@register.filter
def get_item(mapping, key):
    if hasattr(mapping, "get"):
        return mapping.get(key)
    return None


@register.filter
def duration(value) -> str:
    try:
        total = int(float(value or 0))
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(max(0, total), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


@register.filter
def initials(value) -> str:
    text = str(value or "?").strip()
    if not text:
        return "?"
    parts = text.replace("_", " ").replace(".", " ").split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return text[:2].upper()


@register.filter
def percent(value) -> str:
    try:
        return f"{float(value or 0):.0f}%"
    except (TypeError, ValueError):
        return "0%"
