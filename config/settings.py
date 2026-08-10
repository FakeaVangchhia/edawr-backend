"""Django settings — the single place the whole backend is configured.

The mental model: there is no application object you build by hand. Django reads
this module, and everything — installed apps, middleware order, database, DRF
behaviour, commerce rules — is declared here as module-level constants.

Environment variables are read from `.env` (see `.env.example`). Every setting
has a working development default, so the backend runs with no `.env` at all.
The settings that must *not* keep their defaults in production are checked at
startup by `check_production_safety()` in `api/apps.py`, which refuses to boot
rather than letting you deploy them by accident.
"""

import os
import sys
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

# backend/  — every relative path below is resolved against this.
BASE_DIR = Path(__file__).resolve().parent.parent

# Called before any os.getenv() below. It never overwrites a real environment
# variable, so a value set by the container always beats the file.
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


def env_bool(name: str, default: bool) -> bool:
    """Parse a boolean without the classic bug: bool("false") is True.

    Anything unrecognised falls back to the default rather than silently
    reading as False, so a typo in `SECURE_SSL_REDIRECT=ture` does not quietly
    disable HTTPS.
    """
    raw = env(name).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name) or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Environment / debug
# --------------------------------------------------------------------------
# "development" | anything else. One switch drives DEBUG, the security settings
# at the bottom of this file, and the startup safety check.
ENVIRONMENT = env("ENVIRONMENT", "development")
IS_DEVELOPMENT = ENVIRONMENT.lower() == "development"

DEBUG = IS_DEVELOPMENT

# Django refuses requests whose Host header is not listed here (defence against
# host-header poisoning). It matters in practice because the Expo app on a phone
# reaches this server by LAN IP, not "localhost" — with the stock Django default
# that request would 400. "*" is fine for local dev only, and the startup check
# rejects it everywhere else.
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*" if DEBUG else "")


# --------------------------------------------------------------------------
# Auth secrets
# --------------------------------------------------------------------------
# The shipped placeholder, checked into the repo. See check_production_safety().
INSECURE_DEFAULT_JWT_SECRET = "dev-only-insecure-secret-change-me-before-deploying"

JWT_SECRET = env("JWT_SECRET", INSECURE_DEFAULT_JWT_SECRET)
JWT_ALGORITHM = env("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 720)  # 12h

# Django's own secret, used for signing CSRF tokens and anything else Django
# signs. It is deliberately a *different* value from JWT_SECRET: reusing one
# secret across two signing domains means rotating it to fix a leaked API token
# also invalidates every CSRF token, and a leak in either place is a leak in
# both. In development it is derived from JWT_SECRET so there is still nothing
# to configure; in production it must be set explicitly (the startup check
# enforces that).
SECRET_KEY = env("DJANGO_SECRET_KEY") or f"django-derived::{JWT_SECRET}"


# --------------------------------------------------------------------------
# Commerce rules
# --------------------------------------------------------------------------
# The economics of a 10-15 minute grocery run, in one block. `api/pricing.py`
# is the only module that reads these, so changing a fee here changes it in the
# checkout, the storefront's "add ₹X more for free delivery" nudge and the
# stored order total at once.
#
# Money is configured as a string and parsed to Decimal by pricing.money(); a
# float here would reintroduce exactly the rounding error the Decimal columns
# exist to avoid.
DELIVERY_FEE = env("DELIVERY_FEE", "25.00")
FREE_DELIVERY_ABOVE = env("FREE_DELIVERY_ABOVE", "199.00")
HANDLING_FEE = env("HANDLING_FEE", "5.00")
MIN_ORDER_VALUE = env("MIN_ORDER_VALUE", "49.00")

# The promise on the storefront, and the countdown on the tracking screen. Each
# order snapshots this at checkout, so raising it later never rewrites what an
# existing customer was already told.
DELIVERY_PROMISE_MINUTES = env_int("DELIVERY_PROMISE_MINUTES", 15)

# Basket limits. These are abuse guards, not merchandising: without them one
# request can ask the server to lock ten thousand product rows in a single
# transaction.
MAX_ITEMS_PER_ORDER = env_int("MAX_ITEMS_PER_ORDER", 50)
MAX_QUANTITY_PER_ITEM = env_int("MAX_QUANTITY_PER_ITEM", 20)

# How many products one storefront request may return. The catalogue of a dark
# store is small, but "small today" is not a limit.
STORE_PAGE_SIZE = env_int("STORE_PAGE_SIZE", 60)
STORE_MAX_PAGE_SIZE = env_int("STORE_MAX_PAGE_SIZE", 200)

STORE_NAME = env("STORE_NAME", "eDawr")
STORE_CITY = env("STORE_CITY", "Aizawl")


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
# Switching to Postgres is a one-line change:
#   DATABASE_URL=postgres://user:password@localhost:5432/edawr
#
# Do that before taking real orders. SQLite serialises every write against the
# whole database, so two customers checking out at the same moment queue behind
# each other, and `select_for_update()` — which is what stops the last unit of
# stock being sold twice — is a no-op there.
DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL", "sqlite:///./edawr.db"),
        conn_max_age=env_int("DB_CONN_MAX_AGE", 600),
        conn_health_checks=True,
    )
}

# A relative SQLite path would otherwise resolve against the *current working
# directory*, so running a command from the repo root would quietly create a
# second, empty database. Pin it to backend/.
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    name = Path(DATABASES["default"]["NAME"])
    if not name.is_absolute():
        DATABASES["default"]["NAME"] = str(BASE_DIR / name)
    # Wait rather than fail instantly when another connection holds the write
    # lock, and use WAL so reads do not block behind a write. Neither makes
    # SQLite a production database; both make development less annoying.
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"].update({"timeout": 20, "init_command": "PRAGMA journal_mode=WAL;"})

# `id` columns: plain 32-bit AutoField, matching the previous schema. Django's
# own default is BigAutoField; declaring it here silences the startup warning
# and keeps the column type unchanged.
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"


# --------------------------------------------------------------------------
# Cache — and why it is not optional in production
# --------------------------------------------------------------------------
# DRF stores throttle counters in the cache. The default LocMemCache is
# *per-process*, so with four gunicorn workers a "10/min" login limit becomes
# 40/min in practice — and that limit is the only thing standing between a
# four-digit rider PIN and an exhaustive search. Point CACHE_URL at Redis before
# running more than one worker; check_production_safety() refuses to boot
# without it outside development.
CACHE_URL = env("CACHE_URL")

if CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": CACHE_URL,
            "KEY_PREFIX": "edawr",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "edawr-locmem",
        }
    }


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------
# The browser blocks cross-origin requests unless the server opts in. The
# frontend is :3000 and this API is :8000 — different origins, so without this
# every fetch fails. (React Native is not a browser and is unaffected.)
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
)
# This API authenticates with a bearer token in a header, never a cookie, so the
# browser has no credentials to attach. Leaving this on would widen what a
# malicious origin can do for no benefit whatsoever.
CORS_ALLOW_CREDENTIALS = False

# Django validates the Origin of unsafe requests against this list. Same origins
# as CORS, so it is derived rather than configured twice and cannot drift.
CSRF_TRUSTED_ORIGINS = [
    origin for origin in CORS_ALLOWED_ORIGINS if origin.startswith(("http://", "https://"))
]


# --------------------------------------------------------------------------
# Uploads (Django calls user-uploaded files "media")
# --------------------------------------------------------------------------
UPLOAD_DIR = env("UPLOAD_DIR", "uploads")
MEDIA_URL = "/uploads/"
MEDIA_ROOT = BASE_DIR / UPLOAD_DIR

# Django's static file server is single-threaded, does no caching and supports
# no range requests. It is a development convenience. In production put nginx or
# object storage in front of /uploads/ and leave this off; SERVE_MEDIA=true is
# an escape hatch for a single-host deployment that knowingly accepts the cost.
SERVE_MEDIA = env_bool("SERVE_MEDIA", DEBUG)

# Cap what one request may push into memory. The upload view enforces its own
# 5 MB image limit, but that check runs *after* Django has parsed the body —
# these are the limits that stop a 2 GB body from being parsed at all.
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DATA_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200

# Only used by DRF's browsable API stylesheet in development.
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------
# USE_TZ=True makes Django store every datetime in UTC and hand back
# timezone-aware objects. DRF then serialises them as "2026-08-07T10:00:00Z",
# which JavaScript parses correctly. Storing naive datetimes is what produced
# the 5h30m IST offset bug in the FastAPI version.
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
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "api.authentication.AdminJWTAuthentication",
        "api.authentication.RiderJWTAuthentication",
    ],
    # Open by default, locked per view with `permission_classes = [IsAdmin]` or
    # `[IsRider]`. The reverse (deny by default) would be safer, but the login
    # endpoints, the storefront catalogue, checkout and order tracking are all
    # intentionally public, and an explicit AllowAny on them reads as an
    # oversight rather than a decision. See api/permissions.py.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    # ScopedRateThrottle only touches views that set `throttle_scope`, so this
    # is a targeted guard rather than a global limit. AnonRateThrottle is the
    # blanket backstop for everything public.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        # Guards a 4-digit rider PIN: 10,000 possibilities is minutes of
        # unthrottled guessing, and over a fortnight per IP at this rate.
        "login": env("LOGIN_RATE_LIMIT", "10/min"),
        # Checkout writes rows and decrements stock. Without a limit, one script
        # empties the catalogue's stock into fake orders.
        "checkout": env("CHECKOUT_RATE_LIMIT", "12/hour"),
        # Tracking is polled by an open browser tab every few seconds.
        "tracking": env("TRACKING_RATE_LIMIT", "120/min"),
        "anon": env("ANON_RATE_LIMIT", "240/min"),
    },
    # Without this DRF instantiates django.contrib.auth's AnonymousUser for
    # unauthenticated requests. Our "user" is an AdminUser row, so a plain None
    # is both honest and what IsAdmin checks for.
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Guarantees every error body is {"detail": "..."} — the shape the frontend
    # already reads. See api/exceptions.py.
    "EXCEPTION_HANDLER": "api.exceptions.detail_exception_handler",
    # Render Decimal as a JSON number, not a quoted string.
    #
    # DRF's default is a string, on the correct reasoning that JSON numbers are
    # IEEE doubles and a client that parses "0.1" into a float has lost
    # precision. We accept that: these values are displayed, not recomputed —
    # every total the customer is charged is calculated server-side in Decimal
    # and stored. Emitting strings would mean every arithmetic site in the React
    # and React Native apps needs a parseFloat, and the one that gets forgotten
    # concatenates "62" and "35" into "6235".
    "COERCE_DECIMAL_TO_STRING": False,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        # DRF's HTML explorer. Content-negotiated, so it only appears when a
        # browser asks for text/html; fetch() still gets JSON. Development only —
        # in production it is an unnecessary surface that renders user data.
        *(["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    ],
}

SPECTACULAR_SETTINGS = {
    "TITLE": "eDawr API",
    "DESCRIPTION": "Backend for the eDawr storefront, admin console and rider app.",
    "VERSION": "3.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# Interactive docs render every endpoint and invite you to call it. Useful in
# development, an unnecessary map of the attack surface in production.
SERVE_API_DOCS = env_bool("SERVE_API_DOCS", DEBUG)


# --------------------------------------------------------------------------
# Security headers and HTTPS
# --------------------------------------------------------------------------
# Applied in every environment — they cost nothing and are wrong to omit.
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"

if not IS_DEVELOPMENT:
    # Behind a load balancer Django sees plain HTTP and would redirect forever
    # without this header telling it the *original* request was HTTPS. Only
    # trust it when something in front of the app actually sets it — if the app
    # is reachable directly, a client can forge the header and defeat the
    # redirect below.
    if env_bool("TRUST_PROXY_SSL_HEADER", True):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    # A year, with preload. Start lower if you are not yet certain every
    # subdomain can serve HTTPS — HSTS is not something you can take back
    # quickly, because browsers cache it for exactly this long.
    SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
    SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
# Development gets readable lines; production gets one JSON object per line,
# because that is what a log aggregator can index. Tracebacks are logged in
# both — a bare 500 with no stack is a bug you cannot fix.
LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"format": "{levelname:8} {name} {message}", "style": "{"},
        "json": {"()": "config.logformat.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console" if DEBUG else "json",
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        # Django logs handled 4xx here too; ERROR keeps the noise to real faults
        # while still printing the traceback behind every 500.
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "api": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}


# --------------------------------------------------------------------------
# Test run adjustments
# --------------------------------------------------------------------------
# PBKDF2 is deliberately slow — that is the entire point of it, and it must stay
# slow in production. But the test suite hashes a password or a PIN in almost
# every fixture, which turned a 6-second run into a 53-second one. Swapping in a
# fast hasher for tests only is the standard trade: the hashing *code path* is
# still exercised, just with a cheap algorithm.
#
# Keyed off argv rather than an environment variable so it cannot be switched on
# by accident in a deployed process.
TESTING = "test" in sys.argv

if TESTING:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


# --------------------------------------------------------------------------
# Production safety
# --------------------------------------------------------------------------
# The check that refuses to boot on insecure configuration lives in
# `api/apps.py`, not here. `django.conf.settings` only exposes UPPERCASE names,
# so a helper function defined in this module would be invisible to the rest of
# the project — settings files hold values, not behaviour.
