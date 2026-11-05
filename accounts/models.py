"""User model with an explicit, least-privilege role."""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Application user.

    Three roles:
      * ``admin``    – full control, can manage users (via Django admin) and delete data.
      * ``operator`` – can upload footage, start/stop cameras, acknowledge alerts.
      * ``viewer``   – read-only access to dashboards, live streams and reports.
    """

    class Role(models.TextChoices):
        ADMIN = "admin", "Administrator"
        OPERATOR = "operator", "Operator"
        VIEWER = "viewer", "Viewer"

    role = models.CharField(
        max_length=16,
        choices=Role.choices,
        default=Role.VIEWER,
        help_text="Least privilege by default: new accounts can only view data.",
    )

    class Meta(AbstractUser.Meta):
        verbose_name = "user"
        verbose_name_plural = "users"

    def save(self, *args, **kwargs):
        if self.is_superuser:
            self.role = self.Role.ADMIN
        super().save(*args, **kwargs)

    @property
    def is_manager(self) -> bool:
        return self.is_superuser or self.role in {self.Role.ADMIN, self.Role.OPERATOR}

    @property
    def is_admin_role(self) -> bool:
        return self.is_superuser or self.role == self.Role.ADMIN
