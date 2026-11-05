"""WSGI entry point."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chickenvision.settings")

application = get_wsgi_application()
