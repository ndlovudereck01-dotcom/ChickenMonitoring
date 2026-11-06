#!/usr/bin/env bash
# One-shot local setup: venv, dependencies, migrations, demo data.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install --upgrade pip
# CPU wheels first: they satisfy requirements.txt, so pip never downloads the
# multi-gigabyte CUDA builds on a machine without a GPU.
.venv/bin/pip install "torch>=2.4" torchvision --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  .venv/bin/python - <<'PY'
import pathlib, secrets
path = pathlib.Path(".env")
text = path.read_text()
text = text.replace("DJANGO_SECRET_KEY=", f"DJANGO_SECRET_KEY={secrets.token_urlsafe(50)}")
text = text.replace("DJANGO_DEBUG=0", "DJANGO_DEBUG=1")
path.write_text(text)
print("Created .env with a random secret key (DEBUG=1).")
PY
fi

.venv/bin/python manage.py migrate
.venv/bin/python manage.py bootstrap_demo
.venv/bin/python manage.py doctor || true

cat <<'MSG'

Next:
  .venv/bin/python manage.py run_worker     # terminal 1 — detection
  .venv/bin/python manage.py runserver      # terminal 2 — web UI on :8000
MSG
