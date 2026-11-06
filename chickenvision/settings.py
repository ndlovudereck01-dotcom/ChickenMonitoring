"""Django settings for the Chicken Vision monitoring system.

Everything security-relevant is driven by environment variables (see .env.example)
so the same image runs in development and production without code changes.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer, got {raw!r}") from exc


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a number, got {raw!r}") from exc


def env_list(name: str, default: tuple[str, ...] = ()) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if DEBUG:
        # Ephemeral key: fine for a throw-away dev server, never for production.
        from django.core.management.utils import get_random_secret_key

        SECRET_KEY = get_random_secret_key()
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is required when DJANGO_DEBUG is disabled. "
            "Generate one with: python -c \"from secrets import token_urlsafe; "
            "print(token_urlsafe(50))\""
        )

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ("localhost", "127.0.0.1"))
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must list the production hostnames.")

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "accounts",
    "monitoring",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Secure by default: every view needs a session unless it opts out explicitly
    # with @login_not_required (login page, health check).
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "monitoring.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "chickenvision.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "monitoring.context_processors.navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "chickenvision.wsgi.application"
ASGI_APPLICATION = "chickenvision.asgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
if os.environ.get("DB_ENGINE", "sqlite").lower() == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "chickenvision"),
            "USER": os.environ.get("DB_USER", "chickenvision"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"connect_timeout": 5},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(os.environ.get("DB_PATH", BASE_DIR / "db.sqlite3")),
            "OPTIONS": {
                "timeout": 20,
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            },
        }
    }

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

AUTH_PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]

AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env_int("DJANGO_SESSION_COOKIE_AGE", 12 * 60 * 60)
SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool("DJANGO_SESSION_EXPIRE_AT_BROWSER_CLOSE", False)
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # the browser client reads it for fetch() POSTs
CSRF_FAILURE_VIEW = "monitoring.views.csrf_failure"

# ---------------------------------------------------------------------------
# I18N / static / media
# ---------------------------------------------------------------------------
LANGUAGE_CODE = os.environ.get("DJANGO_LANGUAGE_CODE", "en-us")
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Hashed/manifest filenames give long-lived caching in production; during
    # development the plain storage keeps runserver and the test suite working
    # without a collectstatic pass.
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedStaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
# Uploaded videos, annotated outputs and live frames are *private*: they are only
# served through authenticated views, never by the static file handler.
PRIVATE_MEDIA_ROOT = MEDIA_ROOT / "private"
UPLOAD_ROOT = PRIVATE_MEDIA_ROOT / "videos"
OUTPUT_ROOT = PRIVATE_MEDIA_ROOT / "annotated"
THUMBNAIL_ROOT = PRIVATE_MEDIA_ROOT / "thumbnails"
LIVE_FRAME_ROOT = PRIVATE_MEDIA_ROOT / "live"

for _path in (MEDIA_ROOT, PRIVATE_MEDIA_ROOT, UPLOAD_ROOT, OUTPUT_ROOT, THUMBNAIL_ROOT, LIVE_FRAME_ROOT):
    _path.mkdir(parents=True, exist_ok=True)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 500

# ---------------------------------------------------------------------------
# Caching (rate limits, cheap counters). Point this at Redis for multi-process
# deployments: set CACHE_URL=redis://127.0.0.1:6379/1 and install django-redis.
# ---------------------------------------------------------------------------
if os.environ.get("CACHE_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ["CACHE_URL"],
            "TIMEOUT": 300,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "chickenvision",
            "TIMEOUT": 300,
        }
    }

# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 0 if DEBUG else 31_536_000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

TRUST_PROXY_HEADERS = env_bool("DJANGO_BEHIND_TLS_PROXY", False)
if TRUST_PROXY_HEADERS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# ---------------------------------------------------------------------------
# Computer-vision configuration
# ---------------------------------------------------------------------------
MODEL_DIR = BASE_DIR / "models"
MODEL_DIR.mkdir(exist_ok=True)

CHICKEN_MODEL_PATH = os.environ.get("CHICKEN_MODEL_PATH", str(MODEL_DIR / "chicken_yolo11.pt"))
CHICKEN_CLASS_IDS = os.environ.get("CHICKEN_CLASS_IDS", "").strip()
CHICKEN_CONFIDENCE = env_float("CHICKEN_CONFIDENCE", 0.35)
CHICKEN_IOU = env_float("CHICKEN_IOU", 0.50)
CHICKEN_IMGSZ = env_int("CHICKEN_IMGSZ", 640)
CHICKEN_DEVICE = os.environ.get("CHICKEN_DEVICE", "cpu")
CHICKEN_TORCH_THREADS = env_int("CHICKEN_TORCH_THREADS", 2)

CHICKEN_MIN_TRACK_HITS = env_int("CHICKEN_MIN_TRACK_HITS", 5)
CHICKEN_MAX_AGE = env_int("CHICKEN_MAX_AGE", 45)
CHICKEN_N_INIT = env_int("CHICKEN_N_INIT", 3)
CHICKEN_DEDUPE_WINDOW_S = env_float("CHICKEN_DEDUPE_WINDOW_S", 6.0)
CHICKEN_SAMPLE_INTERVAL_S = env_float("CHICKEN_SAMPLE_INTERVAL_S", 1.0)

CHICKEN_MAX_UPLOAD_MB = env_int("CHICKEN_MAX_UPLOAD_MB", 512)
CHICKEN_MAX_VIDEO_SECONDS = env_int("CHICKEN_MAX_VIDEO_SECONDS", 4 * 60 * 60)

CHICKEN_WORKER_CONCURRENCY = env_int("CHICKEN_WORKER_CONCURRENCY", 2)
CHICKEN_WORKER_POLL_SECONDS = env_float("CHICKEN_WORKER_POLL_SECONDS", 2.0)
CHICKEN_LIVE_FPS = env_int("CHICKEN_LIVE_FPS", 10)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "app.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console", "file"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "monitoring": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "ultralytics": {"handlers": ["file"], "level": "WARNING", "propagate": False},
    },
}
