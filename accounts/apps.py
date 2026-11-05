from django.apps import AppConfig
from django.contrib.auth.signals import user_login_failed, user_logged_in
from django.dispatch import receiver


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "Accounts"

    def ready(self) -> None:
        from . import signals  # noqa: F401  (registers receivers)
