#!/usr/bin/env bash
# Start the detection worker. Run it under systemd (or tmux) in production.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
CONCURRENCY="${CHICKEN_WORKER_CONCURRENCY:-2}"

exec "$PYTHON" manage.py run_worker --concurrency "$CONCURRENCY"
