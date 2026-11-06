#!/usr/bin/env bash
# Production web process. Expects TLS termination and static files collected.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
BIND="${BIND:-0.0.0.0:8000}"
WORKERS="${WEB_CONCURRENCY:-3}"
TIMEOUT="${GUNICORN_TIMEOUT:-120}"

"$PYTHON" manage.py migrate --noinput
"$PYTHON" manage.py collectstatic --noinput

exec "$PYTHON" -m gunicorn chickenvision.wsgi:application \
  --bind "$BIND" \
  --workers "$WORKERS" \
  --threads 4 \
  --timeout "$TIMEOUT" \
  --access-logfile - \
  --error-logfile -
