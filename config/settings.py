"""Django settings — the single place the whole backend is configured.

This replaces `app/config.py` (pydantic-settings) *and* the wiring that used to
live in `app/main.py` (CORS middleware, the /uploads static mount, app startup).

The mental shift from FastAPI: there is no application object you build by hand.
Django reads this module, and everything — installed apps, middleware order,
database, DRF behaviour — is declared here as module-level constants.

Environment variables are read from `.env` (see `.env.example`), keeping the
same variable names the FastAPI version used so nothing else had to change.
"""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

# backend/  — every relative path below is resolved against this.
BASE_DIR = Path(__file__).resolve().parent.parent

# pydantic-settings read `.env` for us; here we do it explicitly. Called before
# any os.getenv() below, and it never overwrites a real environment variable.
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# --------------------------------------------------------------------------
# Environment / debug
# --------------------------------------------------------------------------
# "development" | anything else. One switch drives DEBUG and the production
# safety check at the bottom of this file, exactly as it did in app/config.py.
ENVIRONMENT = env("ENVIRONMENT", "development")
IS_DEVELOPMENT = ENVIRONMENT.lower() == "development"

DEBUG = IS_DEVELOPMENT

# Django refuses requests whose Host header is not listed here (defence against
# host-header poisoning). It matters in practice because the Expo app on a phone
# reaches this server by LAN IP, not "localhost" — with the stock Django default
# that request would 400. "*" is fine for local dev only.
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*" if DEBUG else "")


# --------------------------------------------------------------------------
# Auth secrets
# --------------------------------------------------------------------------
# The shipped placeholder, checked into the repo. See check_production_safety().
INSECURE_DEFAULT_JWT_SECRET = "dev-only-insecure-secret-change-me-before-deploying"

JWT_SECRET = env("JWT_SECRET", INSECURE_DEFAULT_JWT_SECRET)
JWT_ALGORITHM = env("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(env("ACCESS_TOKEN_EXPIRE_MINUTES", "720"))  # 12h

# Django's own secret, used for signing cookies/CSRF. This API is stateless and
# uses none of that, but Django requires the setting to be non-empty. Reusing
# JWT_SECRET keeps the number of secrets you have to manage at one.
SECRET_KEY = env("DJANGO_SECRET_KEY") or JWT_SECRET


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------
# Deliberately short. A `django-admin startproject` default also lists
# `django.contrib.admin`, `auth`, `sessions` and `messages` — all of which exist
# to serve HTML pages with cookie sessions. This backend serves JSON to a
# separate Next.js frontend and an Expo app, authenticates with a bearer token,
# and has no server-rendered pages, so none of them are needed.
#
# (Password *hashing* still works: `django.contrib.auth.hashers` is a set of
# plain functions and does not require the `auth` app to be installed.)
INSTALLED_APPS = [
    "django.contrib.contenttypes",  # required by Django's ORM internals
    "django.contrib.staticfiles",   # serves DRF's browsable-API CSS in dev
    "rest_framework",
    "drf_spectacular",              # generates the OpenAPI schema for /docs
    "corsheaders",
    "api",                          # this project's one app
]

# Middleware is an ordered onion: each request passes down the list and each
# response back up it. Order is load-bearing.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Must sit above CommonMiddleware so the CORS preflight (OPTIONS) response
    # gets its headers even when nothing else handles the request.
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Our URLs are declared without trailing slashes because that is what the
# frontend calls (`/api/products`, not `/api/products/`). Turning APPEND_SLASH
# off means a wrong URL 404s honestly instead of being redirected — and a
# redirected POST silently loses its body, which is a miserable bug to chase.
APPEND_SLASH = False

# Only needed by DRF's browsable API. APP_DIRS lets Django find the templates
# that ship inside the rest_framework package.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# Same DATABASE_URL variable as before; `dj_database_url` turns the URL into the
# nested dict Django wants. Switching to Postgres is still a one-line change:
#   DATABASE_URL=postgres://user:password@localhost:5432/edawr
DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL", "sqlite:///./edawr.db"),
        conn_max_age=600,
    )
}

# A relative SQLite path would otherwise resolve against the *current working
# directory*, so running a command from the repo root would quietly create a
# second, empty database. Pin it to backend/.
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    name = Path(DATABASES["default"]["NAME"])
    if not name.is_absolute():
        DATABASES["default"]["NAME"] = str(BASE_DIR / name)

# SQLite's foreign keys are off by default and the setting is per-connection.
# Django issues `PRAGMA foreign_keys=ON` on every SQLite connection itself, so
# the hand-rolled `connect` event listener the SQLAlchemy setup needed is gone.

# `id` columns: plain 32-bit AutoField, matching the previous schema. Django's
# own default is BigAutoField; declaring it here silences the startup warning
# and keeps the column type unchanged.
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------
# The browser blocks cross-origin requests unless the server opts in. The
# frontend is :3000 and this API is :8000 — different origins, so without this
# every fetch fails. (React Native is not a browser and is unaffected.)
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
)
CORS_ALLOW_CREDENTIALS = True


# --------------------------------------------------------------------------
# Uploads (Django calls user-uploaded files "media")
# --------------------------------------------------------------------------
# MEDIA_URL is the public path; MEDIA_ROOT is where bytes land on disk. Keeping
# MEDIA_URL as "/uploads/" preserves the relative `image_url` values already
# stored in the database.
UPLOAD_DIR = env("UPLOAD_DIR", "uploads")
MEDIA_URL = "/uploads/"
MEDIA_ROOT = BASE_DIR / UPLOAD_DIR

# Only used by DRF's browsable API stylesheet in development.
STATIC_URL = "/static/"


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------
# USE_TZ=True makes Django store every datetime in UTC and hand back
# timezone-aware objects. DRF then serialises them as "2026-08-07T10:00:00Z".
#
# That single setting deletes the `ORMModel._serialize_created_at` hack the
# Pydantic schemas needed: SQLAlchemy handed back *naive* datetimes from SQLite,
# which JavaScript parses as local time and rendered 5h30m off in IST.
USE_TZ = True
TIME_ZONE = "UTC"


# --------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    # Runs on every request, before the view. It reads the bearer token and sets
    # request.user / request.auth. It does NOT reject anything — that is the
    # permission class's job. Authentication answers "who is this?", permission
    # answers "may they?".
    # Order matters only for efficiency, not correctness: both classes read the
    # same Bearer header, and each returns None for a token carrying the other's
    # `typ` claim, so whichever runs first cannot swallow the other's tokens.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "api.authentication.AdminJWTAuthentication",
        "api.authentication.RiderJWTAuthentication",
    ],
    # Open by default, locked per view with `permission_classes = [IsAdmin]` or
    # `[IsRider]`. The reverse (deny by default) would be safer, but the two
    # login endpoints and the storefront catalogue are intentionally public, and
    # an explicit AllowAny on them reads as an oversight rather than a decision.
    # See api/permissions.py.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    # ScopedRateThrottle only touches views that set `throttle_scope`, so this
    # is a targeted guard on the two login endpoints rather than a global limit.
    #
    # It matters most for rider sign-in: a 4-digit PIN is 10,000 possibilities,
    # which is minutes of unthrottled guessing. At 10/min an exhaustive search
    # takes over a fortnight per IP. Keyed by IP for anonymous callers, which is
    # the best available signal on a login route — the caller has no identity
    # yet, by definition.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "login": env("LOGIN_RATE_LIMIT", "10/min"),
    },
    # Without this DRF instantiates django.contrib.auth's AnonymousUser for
    # unauthenticated requests. Our "user" is an AdminUser row, so a plain None
    # is both honest and what IsAdmin checks for.
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Guarantees every error body is {"detail": "..."} — the shape the frontend
    # already reads. See api/exceptions.py.
    "EXCEPTION_HANDLER": "api.exceptions.detail_exception_handler",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        # DRF's HTML explorer. Content-negotiated, so it only appears when a
        # browser asks for text/html; fetch() still gets JSON.
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

SPECTACULAR_SETTINGS = {
    "TITLE": "eDawr API",
    "DESCRIPTION": "Backend for the eDawr storefront, admin console and rider app.",
    "VERSION": "2.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}


# --------------------------------------------------------------------------
# Logging — make runserver print tracebacks instead of a bare 500
# --------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        }
    },
}


# --------------------------------------------------------------------------
# Production safety
# --------------------------------------------------------------------------
# The check that refuses to boot on the placeholder JWT_SECRET lives in
# `api/apps.py`, not here. `django.conf.settings` only exposes UPPERCASE names,
# so a helper function defined in this module would be invisible to the rest of
# the project — settings files hold values, not behaviour.
