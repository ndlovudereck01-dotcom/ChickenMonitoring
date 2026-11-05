"""Audit trail for authentication events."""

from __future__ import annotations

import logging

from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

logger = logging.getLogger("monitoring.security")


def _client_ip(kwargs) -> str | None:
    request = kwargs.get("request")
    if request is None:
        return None
    from monitoring.security import get_client_ip

    return get_client_ip(request)


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):  # noqa: ARG001
    from monitoring.utils import audit

    if request is not None:
        audit(request, "auth.login", target=user)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):  # noqa: ARG001
    from monitoring.models import AuditLog

    username = str(credentials.get("username", ""))[:150]
    try:
        AuditLog.objects.create(
            action="auth.login_failed",
            detail=f"username={username}"[:300],
            ip_address=_client_ip({"request": request}),
            user_agent=(request.META.get("HTTP_USER_AGENT", "") if request else "")[:255],
        )
    except Exception:  # pragma: no cover
        logger.exception("Could not record failed login")
