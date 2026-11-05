"""ASGI entry point.

The application is a synchronous WSGI app; this wrapper keeps `daphne`/`uvicorn`
compatible deployments working for the streaming endpoints (MJPEG, SSE).
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chickenvision.settings")

application = get_asgi_application()
