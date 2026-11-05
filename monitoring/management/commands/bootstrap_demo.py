"""Create a usable local environment: roles, cameras and zones.

Safe to run more than once — existing objects are left alone.
"""

from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from monitoring.models import Camera, Zone

User = get_user_model()

DEMO_USERS = [
    ("demo_admin", "Demo", "Administrator", "admin"),
    ("demo_operator", "Demo", "Operator", "operator"),
    ("demo_viewer", "Demo", "Viewer", "viewer"),
]

DEMO_CAMERAS = [
    {
        "name": "House 1 — main floor",
        "location": "House 1",
        "notes": "Wide view of the floor. Draw the ROI just inside the walls.",
        "zones": [
            ("Feeder line A", "feeder", [[0.08, 0.2], [0.42, 0.2], [0.42, 0.55], [0.08, 0.55]]),
            ("Drinkers", "water", [[0.55, 0.18], [0.92, 0.18], [0.92, 0.48], [0.55, 0.48]]),
            ("Pop hole", "door", [[0.42, 0.72], [0.58, 0.72], [0.58, 0.95], [0.42, 0.95]]),
        ],
    },
    {
        "name": "House 2 — brooder",
        "location": "House 2",
        "confidence": 0.3,
        "notes": "Chicks are small: lower the confidence and raise the inference size.",
        "zones": [
            ("Brooder ring", "custom", [[0.2, 0.25], [0.8, 0.25], [0.8, 0.8], [0.2, 0.8]]),
        ],
    },
]


class Command(BaseCommand):
    help = "Seed demo users, cameras and zones for a fresh installation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default="",
            help="Password for the demo accounts. Generated and printed when omitted.",
        )
        parser.add_argument(
            "--no-users",
            action="store_true",
            help="Only create cameras and zones.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        created_accounts = []
        password = options["password"] or secrets.token_urlsafe(12)

        if not options["no_users"]:
            for username, first_name, last_name, role in DEMO_USERS:
                user, created = User.objects.get_or_create(
                    username=username,
                    defaults={"first_name": first_name, "last_name": last_name, "role": role},
                )
                if created:
                    user.set_password(password)
                    user.save(update_fields=["password"])
                    created_accounts.append((username, role))

        created_cameras = 0
        created_zones = 0
        for spec in DEMO_CAMERAS:
            defaults = {
                "location": spec["location"],
                "notes": spec["notes"],
                "confidence": spec.get("confidence", 0.35),
                "is_active": True,
            }
            camera, created = Camera.objects.get_or_create(name=spec["name"], defaults=defaults)
            if created:
                created_cameras += 1
            for name, kind, polygon in spec["zones"]:
                _, zone_created = Zone.objects.get_or_create(
                    camera=camera,
                    name=name,
                    defaults={"kind": kind, "polygon": polygon},
                )
                created_zones += int(zone_created)

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        if created_accounts:
            self.stdout.write("New accounts (password shown once):")
            for username, role in created_accounts:
                self.stdout.write(f"  {username:16} role={role:8} password={password}")
            self.stdout.write(self.style.WARNING("Change these passwords before exposing the app."))
        elif not options["no_users"]:
            self.stdout.write("Demo accounts already existed; passwords unchanged.")

        self.stdout.write(f"Cameras created: {created_cameras}; zones created: {created_zones}.")
        self.stdout.write("")
        self.stdout.write("Next steps:")
        self.stdout.write("  1. python manage.py run_worker        # in a second terminal")
        self.stdout.write("  2. python manage.py runserver 0.0.0.0:8000")
        self.stdout.write("  3. Sign in, open a camera, add a stream URL and start it.")
