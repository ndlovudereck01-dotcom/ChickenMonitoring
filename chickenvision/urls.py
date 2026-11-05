"""Root URL configuration."""

from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from accounts.views import RateLimitedLoginView

urlpatterns = [
    path("", include("monitoring.urls")),
    path("login/", RateLimitedLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(next_page="login"), name="logout"),
    path("admin/", admin.site.urls),
]

admin.site.site_header = "Chicken Vision administration"
admin.site.site_title = "Chicken Vision"
admin.site.index_title = "Farm data"

handler403 = "monitoring.views.permission_denied"
handler404 = "monitoring.views.page_not_found"
handler500 = "monitoring.views.server_error"
