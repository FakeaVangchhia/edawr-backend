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
from corsheaders.defaults import default_headers
from dotenv import load_dotenv

# The repository root — every relative path below is resolved against this.
BASE_DIR = Path(__file__).resolve().parent.parent

# Called before any os.getenv() below. It never overwrites a real environment
# variable, so a value set by the container always beats the file.
#
# ENV_FILE picks which file. `.env` is the development configuration and is
# what `manage.py runserver` reads by default. The deployed configuration is
# kept beside it as `.env.production` -- a Neon database and Render's *internal*
# Redis hostname, which only resolves inside Render's network, so booting from
# it on a laptop turns every throttled request into a ConnectionError 500.
# `ENV_FILE=.env.production` loads it deliberately, to run or diff the live
# configuration locally.
load_dotenv(BASE_DIR / os.getenv("ENV_FILE", ".env"))


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
# Render injects this on every service it runs, and nothing sets it on a
# developer machine. It is read here, well before ALLOWED_HOSTS uses it below,
# because it is the only evidence available this early that we are not on a
# laptop.
RENDER_EXTERNAL_HOSTNAME = env("RENDER_EXTERNAL_HOSTNAME")

# "development" | anything else. One switch drives DEBUG, the security settings
# at the bottom of this file, and the startup safety check.
#
# The default is "development" on a laptop and "production" on Render, and the
# asymmetry is deliberate — it closes the hole that put a debug-mode API on the
# public internet.
#
# Every check in check_production_safety() except the JWT one sits behind
# `if not IS_DEVELOPMENT`. So while this defaulted to "development"
# everywhere, ENVIRONMENT was the one variable whose absence disabled the
# entire safety net: a service created by hand in a dashboard, with nothing
# filled in, booted with DEBUG=True, served Django's traceback page — settings,
# installed apps and all — to anyone who could provoke a 500, fell back to a
# SQLite file on an ephemeral disk, and reported none of it. The checks were
# written to catch exactly that, and could not run.
#
# Defaulting the other way on a host means the failure is the loud kind: the
# deploy refuses to boot and the log names every variable that is missing.


def default_environment(hosted_hostname: str) -> str:
    """What ENVIRONMENT should be when nobody set it.

    A free function taking the hostname rather than reading the module global,
    so `test_startup.py` can assert both directions without reloading this
    module — reloading it would re-run `load_dotenv` and rebind every setting
    for the rest of the suite.
    """
    return "production" if hosted_hostname else "development"


ENVIRONMENT = env("ENVIRONMENT", default_environment(RENDER_EXTERNAL_HOSTNAME))
IS_DEVELOPMENT = ENVIRONMENT.lower() == "development"

DEBUG = IS_DEVELOPMENT

# Django refuses requests whose Host header is not listed here (defence against
# host-header poisoning). It matters in practice because the Expo app on a phone
# reaches this server by LAN IP, not "localhost" — with the stock Django default
# that request would 400. "*" is fine for local dev only, and the startup check
# rejects it everywhere else.
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*" if DEBUG else "")

# Render injects the service's own public hostname, and its health check arrives
# carrying that hostname as the Host header. Without this line the first deploy
# cannot succeed: the URL does not exist until the service does, so there is
# nothing to put in ALLOWED_HOSTS beforehand, a hostname Django rejects makes
# /api/health a 400, only 2xx/3xx pass the check, and the deploy is rolled back
# before you ever reach the dashboard to correct the value.
#
# This *adds* to the configured list rather than replacing it — it is one more
# name the app answers to, not a way to skip setting ALLOWED_HOSTS.
# check_production_safety() still refuses to boot on an empty value or on "*",
# because a service reached through a custom domain must name that domain here.
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)


# --------------------------------------------------------------------------
# Auth secrets
# --------------------------------------------------------------------------
# The shipped placeholder, checked into the repo. See check_production_safety().
INSECURE_DEFAULT_JWT_SECRET = "dev-only-insecure-secret-change-me-before-deploying"

JWT_SECRET = env("JWT_SECRET", INSECURE_DEFAULT_JWT_SECRET)
JWT_ALGORITHM = env("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 720)  # 12h

# How long a *session* may be renewed for, as opposed to how long one token
# lasts.
#
# `/api/auth/me` mints a fresh 12-hour token every time it is called, and both
# clients call it on startup — which is what keeps a manager signed in across a
# shift, and which also means that without a ceiling a session renews itself
# forever. A token copied out of localStorage would then be a permanent
# credential, renewable by the thief on the same 12-hour cadence as the owner.
#
# The `ait` claim records when the session began and survives every refresh;
# past this many hours from it, `/me` refuses and the client has to sign in
# again. A week is long enough that nobody re-enters a password mid-shift or
# even mid-week, and short enough that a leaked token is not forever.
SESSION_MAX_HOURS = env_int("SESSION_MAX_HOURS", 168)  # 7 days

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
FREE_DELIVERY_ABOVE = env("FREE_DELIVERY_ABOVE", "199.00")
HANDLING_FEE = env("HANDLING_FEE", "5.00")
MIN_ORDER_VALUE = env("MIN_ORDER_VALUE", "49.00")

# --- delivery tiers -------------------------------------------------------
# One speed and one delivery charge. There used to be a cheaper, slower "Saver"
# tier as well; it was withdrawn, and `api/pricing.py::delivery_tiers` explains
# what happens to past orders and stale clients that still name it.
#
# The free-delivery threshold below waives this fee once a basket passes it.
DELIVERY_FEE_INSTANT = env("DELIVERY_FEE_INSTANT", "15.00")

# The countdown on the tracking screen. Each order snapshots the minutes of the
# tier it chose, so re-tuning it later never rewrites what an existing customer
# was already told.
DELIVERY_PROMISE_MINUTES_INSTANT = env_int("DELIVERY_PROMISE_MINUTES_INSTANT", 15)

# What a request that names no tier gets. Also what an unrecognised tier falls
# back to — never the cheap one, because silently downgrading someone's delivery
# speed is the failure mode you find out about from a complaint.
DEFAULT_DELIVERY_TYPE = env("DEFAULT_DELIVERY_TYPE", "instant")

# Whether the store picks the rider itself. On, an order reaching Ready is
# handed to the nearest eligible rider in the same transaction (api/dispatch.py)
# and the rider app shows it as already theirs. Off, dispatch falls back to the
# pull feed every rider sees, and somebody has to tap Accept.
#
# It is a switch rather than a constant because the failure it guards against is
# operational, not a bug: on a day when riders are logged in but not actually
# riding, automatic assignment sends orders to phones nobody is looking at, and
# a manager needs to be able to stop that without a deploy.
AUTO_ASSIGN_RIDER = env_bool("AUTO_ASSIGN_RIDER", True)

# Whether the store may wake a rider's phone. See api/push.py.
#
# Off by default, and that is deliberate rather than timid: with it on, every
# order reaching Ready makes an outbound HTTPS call to Expo's servers, and a
# deployment that has not registered a single device would spend that latency
# to send nothing. The mobile app only registers a token once the build has an
# EAS project id, so "on" and "there is something to notify" are the same
# switch. Turn it on when the rider app ships.
PUSH_ENABLED = env_bool("PUSH_ENABLED", False)

# Expo's push gateway. Overridable so the test suite and a staging environment
# can point it at something that is not someone else's production service.
EXPO_PUSH_URL = env("EXPO_PUSH_URL", "https://exp.host/--/api/v2/push/send")

# Optional. Only needed once the Expo project enables "enhanced security" for
# push, which requires the sender to prove it owns the project. Empty means the
# request goes unauthenticated, which is Expo's default and works.
EXPO_ACCESS_TOKEN = env("EXPO_ACCESS_TOKEN")

# A notification is worth a few seconds and not one second more. This bounds a
# call made on a worker thread, so exceeding it costs a missed buzz rather than
# a stalled request — but an unbounded socket to a third party is how a thread
# pool fills up.
PUSH_TIMEOUT_SECONDS = env_int("PUSH_TIMEOUT_SECONDS", 8)

# --------------------------------------------------------------------------
# Live location
# --------------------------------------------------------------------------
# How old a rider's last position may be and still be shown as live.
#
# The rider app reports from the foreground only, so a phone that goes into a
# pocket stops sending and starts ageing. This is therefore the line between
# "moving" and "last seen", not between working and not: past it the console
# shows a last-known position with its age, and the customer's tracking page
# shows nothing at all rather than a marker that has quietly stopped being true.
#
# Ninety seconds is six missed reports at the app's ten-second cadence — long
# enough to ride through a dead spot on the Durtlang road, short enough that a
# stopped marker is noticed while the delivery is still happening.
LOCATION_STALE_SECONDS = env_int("LOCATION_STALE_SECONDS", 90)

# How long the per-order breadcrumb trail is kept, in days.
#
# The trail exists to settle "nobody came" after a failed delivery, and that
# conversation happens within days, not months. It is deleted by
# `manage.py prune_locations`, which must be scheduled — see deployment.md.
# Nothing else removes it, and a position history nothing removes is a growing
# record of where customers live kept for no stated purpose.
LOCATION_PING_RETENTION_DAYS = env_int("LOCATION_PING_RETENTION_DAYS", 30)

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

# The wall-clock timezone the store actually trades in.
#
# `TIME_ZONE` above stays UTC and every row is stored in UTC — that is correct
# and is not what this is for. This is the timezone the *analytics* group by, so
# that "today's revenue" means the day the shopkeeper is having. Aizawl is
# UTC+5:30, so bucketing by UTC dates would file every order placed after 18:30
# local under tomorrow, and the daily chart would be wrong by a third of each
# evening's trade — the busiest part of it.
STORE_TIMEZONE = env("STORE_TIMEZONE", "Asia/Kolkata")


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
    # Without this, the X_FRAME_OPTIONS setting below is inert — the setting
    # only names the value; this middleware is what attaches the header.
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# `django.middleware.csrf.CsrfViewMiddleware` is deliberately absent, and
# `manage.py check --deploy` will warn about that (security.W003). CSRF is an
# attack on *ambient* credentials: it works because a browser attaches cookies
# to a cross-site request automatically. This API authenticates with a bearer
# token that JavaScript has to attach deliberately, has no session or login
# cookie, and sets CORS_ALLOW_CREDENTIALS = False — so there is nothing for a
# forged cross-site request to ride on. Adding the middleware would break every
# client for no gain. Revisit the moment anything here starts using a cookie.

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
# Postgres in production and in CI; SQLite only as the zero-configuration
# development fallback. SQLite serialises every write against the whole
# database, and `select_for_update()` — which is what stops the last unit of
# stock being sold twice — is a no-op there, so `check_production_safety()`
# refuses to boot on it outside development.
DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL", "sqlite:///./edawr.db"),
        conn_max_age=env_int("DB_CONN_MAX_AGE", 600),
        conn_health_checks=True,
    )
}

# A relative SQLite path would otherwise resolve against the *current working
# directory*, so running a command from elsewhere would quietly create a
# second, empty database. Pin it to the repository root.
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

# A cross-origin response's custom headers are invisible to JavaScript unless
# they are named here — the browser sends them and then refuses to let `fetch`
# read them. The admin console pages its tables on `X-Total-Count`, so without
# this the header arrives, is silently unreadable, and every list falls back to
# "the total is however many rows are on this page". No error, no warning, just
# a paginator that always claims one page.
CORS_EXPOSE_HEADERS = ["X-Total-Count"]

# The request-side twin of the above. Checkout sends `Idempotency-Key`, and a
# header outside django-cors-headers' default list fails the preflight — the
# browser then never sends the POST at all, so the storefront shows "Could not
# reach the store" and the API logs nothing but an OPTIONS. Every web checkout
# failed this way in production. React Native skips preflights, which is why
# the apps kept working and hid it.
CORS_ALLOW_HEADERS = (*default_headers, "idempotency-key")

# No CSRF_TRUSTED_ORIGINS here on purpose. It is only consulted by
# CsrfViewMiddleware, which this project does not install (see the note beside
# MIDDLEWARE). Setting it anyway would look like a control that is doing
# something when it is doing nothing at all — which is worse than its absence,
# because the next person reads it and stops looking.


# --------------------------------------------------------------------------
# Uploads (Django calls user-uploaded files "media")
# --------------------------------------------------------------------------
UPLOAD_DIR = env("UPLOAD_DIR", "uploads")
MEDIA_URL = "/uploads/"
MEDIA_ROOT = BASE_DIR / UPLOAD_DIR

# Where uploaded images are written: "local" (a directory) or "r2" (Cloudflare
# R2, over its S3 API). See api/storage.py.
#
# **The value stored in the database is identical either way** — the relative
# path "/uploads/<name>", which is also the R2 object key with a slash in front.
# So this switch moves where new bytes land and nothing else; no row changes,
# and the hostname stays out of the database.
#
# Defaults to local, so a checkout with no R2 credentials runs.
UPLOAD_BACKEND = env("UPLOAD_BACKEND", "local").lower()

# --------------------------------------------------------------------------
# Phone verification
# --------------------------------------------------------------------------
# Which backend sends a one-time code. See api/sms.py.
#
#     disabled  refuses, loudly. The default, and correct until a provider
#               exists: `Customer.phone_verified_at` stays null, and an
#               unverified account sees only the orders linked to it, which is
#               the behaviour the application has always had.
#     console   logs the message instead of sending it. Development only, and
#               check_production_safety() refuses to boot on it — a working
#               one-time code in a production log is readable by anyone with a
#               dashboard login, and the endpoint would answer 200 while the
#               customer received nothing.
SMS_BACKEND = env("SMS_BACKEND", "disabled").lower()

# How long a challenge is good for. Ten minutes is the usual compromise: long
# enough to fetch a phone from another room, short enough that a code read off a
# lock screen by somebody else has usually expired.
OTP_TTL_SECONDS = env_int("OTP_TTL_SECONDS", 600)

# Digits in the code. Six is what Indian customers expect from every other app,
# and the length is only half the security story anyway — **the throttle is the
# other half and it is the load-bearing one.** The challenge is stateless (a
# signed token, no row), so there is no attempt counter to increment and nothing
# to lock out; what stops a million guesses at a six-digit code is the `otp`
# scope below. Raising this without also tightening that would be theatre.
OTP_CODE_DIGITS = env_int("OTP_CODE_DIGITS", 6)

# R2. The AWS_* spellings are read as a fallback because that is what the S3
# API calls them and what the Cloudflare dashboard hands you.
#
# **The endpoint must not name the bucket.** Cloudflare shows an "S3 API"
# address ending in /<bucket>; boto3 wants the account host alone and appends
# the bucket itself, so the dashboard value pasted straight in produces
# requests for /edawr/edawr/uploads/... and 404s that look like missing files.
# Deriving it from the account id sidesteps that entirely.
R2_ACCOUNT_ID = env("R2_ACCOUNT_ID") or env("AWS_ACCOUNT_ID")
R2_ENDPOINT_URL = env("R2_ENDPOINT_URL") or (
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
)
R2_BUCKET = env("R2_BUCKET") or env("AWS_BUCKET_NAME")
R2_ACCESS_KEY_ID = env("R2_ACCESS_KEY_ID") or env("AWS_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = env("R2_SECRET_ACCESS_KEY") or env("AWS_SECRET_ACCESS_KEY")

# Where a browser reads the objects back from: the bucket's r2.dev subdomain,
# or a custom domain once one is connected. An R2 bucket is private until one of
# those is turned on, so without this every product image 404s while uploads
# quietly succeed.
#
# The clients need the same value as NEXT_PUBLIC_MEDIA_URL / EXPO_PUBLIC_MEDIA_URL
# — they, not the serializers, are what puts the host in front of the stored
# path. Nothing on this side depends on it except management commands, which is
# why check_production_safety() has to be the thing that notices it is missing.
R2_PUBLIC_BASE_URL = env("R2_PUBLIC_BASE_URL").rstrip("/")

# Django's static file server is single-threaded, does no caching and supports
# no range requests. It is a development convenience, and with UPLOAD_BACKEND=r2
# it serves only whatever legacy files are still on the disk — new uploads never
# touch it. SERVE_MEDIA=true is an escape hatch for a single-host deployment
# that knowingly accepts the cost.
SERVE_MEDIA = env_bool("SERVE_MEDIA", DEBUG)

# The operator's statement that MEDIA_ROOT survives a deploy.
#
# It exists because "local" is the default and a container filesystem is not a
# disk. Uploaded product photos written there are gone on the next deploy, and
# nothing notices: the upload returns 200, the row keeps its /uploads/<name>,
# the console shows the picture until the pod restarts, and the storefront
# starts 404ing every image the following morning. The bytes are unrecoverable —
# somebody has to photograph the shelf again.
#
# So outside development check_production_safety() refuses UPLOAD_BACKEND=local
# unless this says the directory is genuinely persistent. A mounted volume is a
# legitimate deployment (render.yaml still mounts one), which is why this is an
# acknowledgement rather than a ban — but it has to be typed, because the
# failure it prevents is silent and permanent.
UPLOAD_DISK_PERSISTENT = env_bool("UPLOAD_DISK_PERSISTENT", False)

# Cap what one request may push into memory. The upload view enforces its own
# 5 MB image limit, but that check runs *after* Django has parsed the body —
# these are the limits that stop a 2 GB body from being parsed at all.
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DATA_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------
# Where `manage.py backup_database` writes its pg_dump archives, and how many it
# keeps.
#
# **A local tool now, not a production one.** Render's native runtime has no
# `apt-get`, so `pg_dump` is not there and this command cannot run in the
# deployed service. Production backups are Neon's point-in-time recovery. Run
# this from a machine that has the PostgreSQL client tools when you want a copy
# you hold yourself — before a risky migration is the usual reason.
#
# **It must not be MEDIA_ROOT or anything beneath it.** Production runs with
# SERVE_MEDIA=true, so Django serves every file under MEDIA_ROOT to anyone who
# asks, and a database dump written there would be a public download of every
# customer's name, phone number and address. The command refuses to run rather
# than relying on this comment being read.
BACKUP_DIR = env("BACKUP_DIR", "backups")
BACKUP_KEEP = env_int("BACKUP_KEEP", 14)

# Only used by DRF's browsable API stylesheet in development. Nothing runs
# `collectstatic`, so there is no STATIC_ROOT.
STATIC_URL = "/static/"


# --------------------------------------------------------------------------
# Password strength
# --------------------------------------------------------------------------
# Read by `api/security.validate_password_strength`, which every path that
# *sets* a customer password goes through. Django's validators are plain
# functions and work without `django.contrib.auth` in INSTALLED_APPS, the same
# way its hashers do — four rules and their wording for nothing.
#
# Staff passwords do not currently go through this: an admin account is created
# by `manage.py seed_admin`, which enforces its own minimum, and a rider signs
# in with a PIN whose whole point is that it is four digits. Routing those
# through here would be an improvement; it would also lock out existing rows,
# so it is a separate decision from adding customers.
AUTH_PASSWORD_VALIDATORS = [
    # Reads `phone` and `name` off the unsaved instance handed to the validator,
    # so "9812345678" is refused as the password for +919812345678. This is the
    # single most likely weak password on a phone-keyed account.
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    # Ships a 20,000-word list. Blocks "password", "12345678" and "qwerty123"
    # without anyone maintaining a list here.
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    # The one that earns its place in *this* product. A customer identified by
    # a phone number, asked for a password, reaches for ten digits.
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------
# USE_TZ=True makes Django store every datetime in UTC and hand back
# timezone-aware objects. DRF then serialises them as "2026-08-07T10:00:00Z",
# which JavaScript parses correctly. A naive datetime would be read as local
# time by one side and UTC by the other — a 5h30m error on every timestamp.
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
        # Last, so staff requests still stop at their own class. Three classes
        # means up to three signature verifications for a customer request,
        # which is cheap next to the query that follows.
        "api.authentication.CustomerJWTAuthentication",
    ],
    # Open by default, locked per view with `permission_classes = [IsAdmin]` or
    # `[IsRider]`. The reverse (deny by default) would be safer, but the login
    # endpoints, the storefront catalogue, checkout and order tracking are all
    # intentionally public, and an explicit AllowAny on them reads as an
    # oversight rather than a decision. See api/permissions.py.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    # Four classes that between them cover every request exactly once.
    #
    # ScopedRateThrottle only touches views that set `throttle_scope`, so it is
    # a targeted guard rather than a global limit. AnonRateThrottle is the
    # blanket backstop for everything public — and it returns None the moment a
    # request carries credentials. StaffRateThrottle and CustomerRateThrottle
    # are the mirror image: each returns None unless the caller is its own kind
    # of account, and keys on the account when it is.
    #
    # **"Exactly once" is a property of this list, not of any view**, and it is
    # what breaks when a new kind of authenticated caller is added. Anonymous →
    # Anon; admin or rider → Staff; customer → Customer. A fifth identity with
    # no class of its own would be metered by nothing at all, because the two
    # blanket classes both step aside for anything they do not recognise. That
    # has now happened twice; see api/throttling.py.
    #
    # The Scoped class is ours rather than DRF's because DRF's keys on a bare
    # `request.user.pk`, which merges an admin and a customer who happen to
    # share a primary key on the two scopes both can reach.
    #
    # StaffRateThrottle is listed *here* rather than on each authenticated view
    # because putting it on the view is a thing you have to remember. It was on
    # AdminAPIView and OwnerAdminAPIView only, which left ten authenticated
    # endpoints — including the rider dashboard the class was written for, and
    # the O(n)-query `?stalled=true` — entirely unmetered, so one valid token
    # was an unlimited channel. A default cannot be forgotten by the next
    # endpoint.
    "DEFAULT_THROTTLE_CLASSES": [
        "api.throttling.NamespacedScopedRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
        "api.throttling.StaffRateThrottle",
        "api.throttling.CustomerRateThrottle",
    ],
    # How many proxies sit in front of this app — and without it, none of the
    # rates below exist.
    #
    # DRF identifies an anonymous caller in BaseThrottle.get_ident(). Left at its
    # default of None, that method returns the *entire* X-Forwarded-For header as
    # the throttle key. The header is client-supplied, so an attacker sends a
    # different value on every request and gets a fresh bucket each time: the
    # login, checkout, tracking and anon limits all become decoration. Set to an
    # integer, DRF instead takes the entry the trusted proxy itself appended and
    # ignores anything the client prepended.
    #
    # Wrong in either direction hurts, which is why this is configurable:
    #   too high — you trust hops that do not exist, and read a forged address;
    #   too low  — you key on the proxy's address, and every customer behind one
    #              CDN or carrier NAT shares a single bucket.
    # 1 matches Render, which terminates TLS at its proxy and adds exactly one
    # hop (the same trust decision as TRUST_PROXY_SSL_HEADER). 0 is correct in
    # development, where runserver is reached directly and there is no proxy to
    # trust — REMOTE_ADDR is already the client.
    "NUM_PROXIES": env_int("NUM_PROXIES", 0 if IS_DEVELOPMENT else 1),
    "DEFAULT_THROTTLE_RATES": {
        # Guards a 4-digit rider PIN: 10,000 possibilities is minutes of
        # unthrottled guessing, and over a fortnight per IP at this rate.
        "login": env("LOGIN_RATE_LIMIT", "10/min"),
        # Customer sign-up and sign-in. The same number as `login`, and
        # deliberately a *separate budget* rather than a share of that one.
        # Both are keyed on the IP address, because neither carries a token
        # yet — so on a carrier NAT in Aizawl, one customer mistyping their
        # password would otherwise eat the bucket that lets a rider sign in to
        # start their shift. Staff being unable to work is a worse outcome than
        # a shopper waiting a minute.
        "customer_auth": env("CUSTOMER_AUTH_RATE_LIMIT", "10/min"),
        # Checkout writes rows and decrements stock. Without a limit, one script
        # empties the catalogue's stock into fake orders.
        "checkout": env("CHECKOUT_RATE_LIMIT", "12/hour"),
        # Tracking is polled by an open browser tab every few seconds.
        "tracking": env("TRACKING_RATE_LIMIT", "120/min"),
        # The poll sticker on the storefront home. It writes a row from an
        # anonymous caller, so it is metered like checkout rather than like a
        # read. Keyed by IP for guests, and — as `customer_auth` notes above —
        # an IP in Aizawl is a carrier NAT shared by a street, so the budget
        # assumes the key is shared: thirty is a whole neighbourhood's worth
        # of answers an hour and still nothing to a script filling the table.
        "suggestions": env("SUGGESTIONS_RATE_LIMIT", "30/hour"),
        # Phone verification, keyed per customer account. Deliberately tight,
        # and it is doing two different jobs at two different endpoints:
        # on `challenge` it is what stops the API being used to send somebody
        # else's phone a message every few seconds, and on `verify` it *is* the
        # attempt counter — the challenge is a signed token rather than a row,
        # so there is nothing to increment and nothing to lock out. Ten an hour
        # against a six-digit code is 0.001% of the space per hour.
        "otp": env("OTP_RATE_LIMIT", "10/hour"),
        "anon": env("ANON_RATE_LIMIT", "240/min"),
        # Crash and CSP reports. Public and unauthenticated, because a crash
        # report is worth having precisely when nobody is signed in. Generous
        # rather than tight — a genuinely broken deploy produces a burst of
        # them, and that burst is the signal, not abuse — but bounded, because
        # log storage anyone can write to without limit costs real money.
        "reports": env("REPORT_RATE_LIMIT", "60/min"),
        # Authenticated staff: the admin console and the rider app. Until this
        # existed, AnonRateThrottle returned None the moment a request carried a
        # token, so one valid credential was an unmetered channel into every
        # endpoint. Generous on purpose — both clients poll on a timer and a
        # manager working a rush legitimately hits many endpoints a minute. The
        # point is a ceiling, not a budget. Keyed per account, namespaced by
        # table, in api/throttling.py.
        "staff": env("STAFF_RATE_LIMIT", "600/min"),
        # A rider reporting their position. Roughly 6/min at the app's cadence;
        # this leaves room for the burst that arrives when a handset comes out
        # of a dead spot with queued fixes.
        #
        # **A separate scope, not a share of `staff`.** Both throttles apply to
        # this endpoint, and that is the point: without its own bucket, a
        # location loop that wedges on a retry would spend the rider's whole
        # 600/min ceiling and the next thing they could not do is accept an
        # order. Telemetry must not be able to starve the work.
        "rider_location": env("RIDER_LOCATION_RATE_LIMIT", "30/min"),
        # The customer sharing their own position from the tracking page. Same
        # number, and unauthenticated — the tracking token is the only
        # credential — so `AnonRateThrottle` meters it as well.
        "customer_location": env("CUSTOMER_LOCATION_RATE_LIMIT", "30/min"),
        # A signed-in customer, keyed per account. **Deliberately identical to
        # `anon`**: signing in must not change how much of this API you can
        # consume. Tighter, and signing in is a downgrade that teaches people to
        # sign out; looser, and an account becomes a way to buy capacity. Not
        # `staff`, which is generous because the console and the rider app both
        # poll dashboards — a customer polls one order.
        "customer": env("CUSTOMER_RATE_LIMIT", "240/min"),
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
    # `payment_method` appears on both the Order and the checkout request with
    # the same choices. Without a name, drf-spectacular invents one per
    # occurrence ("PaymentMethodC24Enum"), which is both ugly and unstable —
    # generated clients would rename the type whenever the hash changed.
    "ENUM_NAME_OVERRIDES": {
        "PaymentMethodEnum": "api.models.Order.PAYMENT_CHOICES",
        "OrderStatusEnum": "api.models.Order.STATUS_CHOICES",
        "DeliveryTypeEnum": "api.models.Order.DELIVERY_TYPE_CHOICES",
        "UserRoleEnum": "api.models.User.ROLE_CHOICES",
        # Products and categories share one status vocabulary (api.models
        # .STATUS_CHOICES), so this is deliberately *one* entry. Registering the
        # same choice set under two names is an error, not a convenience.
        "CatalogueStatusEnum": "api.models.STATUS_CHOICES",
        "AdminRoleEnum": "api.models.AdminUser.ROLE_CHOICES",
        "AuditActionEnum": "api.models.AuditLog.ACTION_CHOICES",
        "AuditActorEnum": "api.models.AuditLog.ACTOR_CHOICES",
    },
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

# Silenced because it is a considered decision, not an oversight — see the note
# beside MIDDLEWARE for why CSRF middleware does not belong in a stateless
# bearer-token API. Recording it here means `manage.py check --deploy` comes
# back clean, so the next real warning is not lost in a known one.
SILENCED_SYSTEM_CHECKS = ["security.W003"]

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

    # Set for correctness rather than effect: this project installs neither the
    # sessions app nor the CSRF middleware, so neither cookie is ever issued.
    # They are here so that the day something does set a cookie, it is already
    # marked Secure instead of relying on someone noticing.
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

    # **Never the real bucket.** A developer whose .env carries UPLOAD_BACKEND=r2
    # — which is what production runs, and what you set to reproduce it — would
    # otherwise have the upload suite write objects into the live store and the
    # cleanup tests delete from it. The failure is silent in the sense that
    # matters: every test still passes.
    #
    # The R2 backend is exercised by api/tests/test_uploads.py against a stub
    # client, under an explicit override_settings. That is the only place it
    # runs in a test.
    UPLOAD_BACKEND = "local"

    # The suite exercises the whole verification flow, and `disabled` — the
    # production default — raises before a code is ever issued. `console` keeps
    # the send inside the process, where it writes a log line and nothing
    # leaves the machine.
    SMS_BACKEND = "console"

    # **Never the real cache, for the same reason.** `api/tests/base.py` calls
    # `cache.clear()` after every test so throttle counters do not leak between
    # them. Against a `.env` that carries production's CACHE_URL that is a
    # FLUSHDB on the live Redis, six hundred times a run — every login and
    # checkout budget reset, in the one store that is open. And when the host is
    # unreachable from a laptop, which is the usual case, every test errors on
    # connect before it starts. LocMem is what the suite was written against.
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "edawr-test",
        }
    }

    # Two adjustments that only make `manage.py test` work at all against a
    # managed Postgres. Both are inside `if TESTING`, so a deployed process is
    # untouched — and TESTING is keyed off argv, so it cannot be switched on by
    # accident.
    #
    # **The test database is created and dropped through the DIRECT endpoint.**
    # Neon's pooled host (`-pooler` in the name) keeps server-side connections
    # open on the pooler's own schedule, and PostgreSQL refuses to drop a
    # database that anything is connected to. Through the pooler the drop is not
    # slow — it never succeeds, and every later run stops on "database
    # test_neondb already exists ... is being accessed by other users" until
    # somebody terminates those sessions by hand. Nothing is lost by going
    # direct: the test database is a scratch database this process just created,
    # and it is the only client. `neondb` itself is never touched by the test
    # runner, which works on `test_neondb` from creation to drop.
    #
    # A host with no `-pooler.` in it — CI's service container, a local server,
    # SQLite's empty HOST — is left exactly as it was.
    _host = DATABASES["default"].get("HOST") or ""
    if "-pooler." in _host:
        DATABASES["default"]["HOST"] = _host.replace("-pooler.", ".", 1)

    # **No persistent connections while testing.** DB_CONN_MAX_AGE defaults to
    # 600, which is right for a web worker and wrong here: a suite that is
    # interrupted — Ctrl-C, a killed process, a debugger — leaves connections
    # alive for ten more minutes, and every one of them blocks the DROP DATABASE
    # the next run begins with. 0 closes each connection as it is finished with,
    # so an abandoned run leaves nothing behind to clean up.
    DATABASES["default"]["CONN_MAX_AGE"] = 0


# --------------------------------------------------------------------------
# Production safety
# --------------------------------------------------------------------------
# The check that refuses to boot on insecure configuration lives in
# `api/apps.py`, not here. `django.conf.settings` only exposes UPPERCASE names,
# so a helper function defined in this module would be invisible to the rest of
# the project — settings files hold values, not behaviour.
