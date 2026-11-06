"""Authentication views with brute-force throttling."""

from __future__ import annotations

from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import LoginView
from django.shortcuts import render
from django.utils.decorators import method_decorator

from monitoring.security import is_rate_limited

LOGIN_ATTEMPT_LIMIT = 8
LOGIN_ATTEMPT_WINDOW = 300  # five minutes


# The login page itself must stay reachable: LoginRequiredMiddleware guards every
# other view, and without this exemption anonymous visitors would loop forever.
@method_decorator(login_not_required, name="dispatch")
class RateLimitedLoginView(LoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST" and is_rate_limited(
            "login", request, limit=LOGIN_ATTEMPT_LIMIT, window=LOGIN_ATTEMPT_WINDOW
        ):
            return render(
                request,
                self.template_name,
                {
                    "form": self.get_form(),
                    "throttled": True,
                    "next": request.GET.get("next", ""),
                },
                status=429,
            )
        return super().dispatch(request, *args, **kwargs)
