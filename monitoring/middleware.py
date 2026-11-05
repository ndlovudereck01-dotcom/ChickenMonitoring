"""Defence-in-depth HTTP security headers."""

from __future__ import annotations

from django.conf import settings


class SecurityHeadersMiddleware:
    """Adds a strict Content-Security-Policy and related headers.

    The application ships no inline ``<script>`` or ``<style>`` blocks, so the
    policy can forbid inline code entirely. Django's admin is excluded because it
    relies on a few inline handlers of its own.
    """

    CSP = (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "script-src 'self'; "
        "style-src 'self'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "worker-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        is_admin = request.path.startswith("/admin/")
        if not is_admin:
            response.setdefault("Content-Security-Policy", self.CSP)
        response.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if not settings.DEBUG:
            response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response
