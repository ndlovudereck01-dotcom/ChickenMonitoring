"""Small security helpers: client IP resolution, throttling and RBAC decorators."""

from __future__ import annotations

import functools
import logging
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

logger = logging.getLogger("monitoring.security")

# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------


def get_client_ip(request) -> str | None:
    """Return the real client IP.

    ``X-Forwarded-For`` is only trusted when the app is explicitly deployed
    behind a TLS-terminating proxy, otherwise a client could spoof its address
    and evade the login throttle.
    """
    if getattr(settings, "TRUST_PROXY_HEADERS", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return request.META.get("REMOTE_ADDR") or None


# ---------------------------------------------------------------------------
# Fixed-window rate limiting
# ---------------------------------------------------------------------------


def is_rate_limited(scope: str, request, *, limit: int, window: int = 60) -> bool:
    """Count a hit in the current window; True when the caller exceeded ``limit``."""
    identity = get_client_ip(request) or "anonymous"
    key = f"ratelimit:{scope}:{identity}"
    try:
        added = cache.add(key, 1, timeout=window)
        count = 1 if added else cache.incr(key)
    except (ValueError, NotImplementedError):  # cache entry expired between calls
        cache.set(key, 1, timeout=window)
        count = 1
    if count > limit:
        logger.warning("Rate limit hit: scope=%s identity=%s count=%s", scope, identity, count)
        return True
    return False


def ratelimit(scope: str, *, limit: int, window: int = 60):
    """View decorator returning HTTP 429 when ``limit`` is exceeded."""

    def decorator(view: Callable):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if is_rate_limited(scope, request, limit=limit, window=window):
                response = HttpResponse(
                    "Too many requests. Please slow down and try again shortly.",
                    status=429,
                    content_type="text/plain; charset=utf-8",
                )
                response["Retry-After"] = str(window)
                return response
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


# ---------------------------------------------------------------------------
# Role based access control
# ---------------------------------------------------------------------------


def role_required(*roles: str):
    """Allow only the given roles (superusers always pass)."""

    def decorator(view: Callable):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect(f"{reverse('login')}?next={request.path}")
            if user.is_superuser or user.role in roles:
                return view(request, *args, **kwargs)
            logger.warning(
                "Permission denied: user=%s role=%s view=%s", user.pk, user.role, view.__name__
            )
            raise PermissionDenied("Your role does not allow this action.")

        return wrapped

    return decorator


def manager_required(view: Callable):
    """Operator or administrator."""
    from accounts.models import User

    return role_required(User.Role.ADMIN, User.Role.OPERATOR)(view)


def admin_required(view: Callable):
    """Administrator only."""
    from accounts.models import User

    return role_required(User.Role.ADMIN)(view)
