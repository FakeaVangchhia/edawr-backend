"""Gunicorn's configuration — how this backend is actually served.

`manage.py runserver` prints a warning telling you not to use it in production.
It is right, and this file is the other half of that sentence: `render.yaml`'s
`startCommand` points here, so the production server is configured somewhere
that can carry its reasoning rather than in a shell line that cannot.

**WSGI rather than ASGI, and that is a decision rather than an omission.**

Every view in this project is synchronous. The ORM calls are sync, `psycopg` is
sync, and the one outbound HTTP call — `api/push.py` — already runs on a thread
of its own precisely so that no request waits on it. Serving a fully synchronous
application over ASGI does not make any of that concurrent: Django hands each
sync view to a thread from a pool, so you get the same thread-per-request
behaviour as below, with an event loop and a `sync_to_async` hop underneath it,
and a class of bug (`SynchronousOnlyOperation`) that WSGI cannot produce.

ASGI earns its place when there is something to *await*. For this store that
means one of: a websocket or SSE stream, so the tracking page stops polling; an
SMS or payment provider on the request path, where a thread would otherwise sit
idle holding a database connection; or fan-out to several services per request.
None of those exist yet. `config/asgi.py` is already written for the day one
does, and `worker_class` below is most of what changes when it arrives.

Note for anyone developing on Windows: gunicorn is POSIX-only — it needs `fork`
and `fcntl` — so nothing here can be run or checked natively. `manage.py
runserver` is the everyday answer, and the warning it prints is about the
existence of this file rather than about a problem with your machine.

`uv run waitress-serve` (dev group, see `config/wsgi.py`) gives you the
application without an auto-reloader, but reads none of this file. **Render is
the only place these values are actually applied**, so a change here is checked
by deploying it — or on any Linux machine with `uv run gunicorn --config
config/gunicorn.py`.
"""

import os
import sys

# Gunicorn execs this file *before* it puts the working directory on sys.path
# (`Application.chdir()` runs after the config is loaded), so `config.settings`
# is not importable yet. Adding backend/ here is what makes the import below
# work — it is load-bearing, not tidying.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Imported rather than re-implemented, so the two layers cannot disagree about
# what a value means: `env_bool` here refuses to read a typo'd "ture" as False
# exactly as it does for Django. Importing the module also loads backend/.env,
# so a deployment that is not a container still reads the same values Django
# will.
from config.settings import env, env_bool, env_int  # noqa: E402


# --------------------------------------------------------------------------
# What to serve, and where
# --------------------------------------------------------------------------
# Named here rather than as a CMD argument, so one place knows it.
wsgi_app = "config.wsgi:application"

# Render injects PORT and requires the service to listen on it. Binding a fixed
# port instead is the classic way to deploy something that builds, starts, and
# then fails every health check.
bind = f"0.0.0.0:{env_int('PORT', 8080)}"


# --------------------------------------------------------------------------
# Workers — and the number this is really setting
# --------------------------------------------------------------------------
# `gthread`: a fixed pool of threads per worker, one per in-flight request. The
# alternative for a sync app is `sync`, which serves exactly one request per
# worker at a time — on a single-CPU instance that puts the whole store in a
# queue behind one slow query.
worker_class = "gthread"

# One worker per CPU is the formula, and Render's Starter instance has half of
# one (0.5 CPU, 512 MB). More processes on one core buys nothing but memory:
# they duplicate the ~80 MB interpreter and then compete for the same CPU.
# Raise this only alongside the instance type, not instead of it.
workers = env_int("WEB_CONCURRENCY", 1)

# These requests are IO-bound — Postgres and Redis are both network hops — so
# threads, not processes, are the resource that matters here.
#
# **workers × threads is also the ceiling on database connections this instance
# holds**, and `DB_CONN_MAX_AGE=600` keeps each open for ten minutes past its
# last use. One instance is therefore 1 × 8 = 8 connections held open — which is
# why `DATABASE_URL` should be Neon's **pooled** endpoint, the one with `-pooler`
# in the host. Neon's own limit is on direct connections; the pooler exists so a
# long-lived application can hold handles without spending them. Raising this is
# a database decision at least as much as a throughput one.
threads = env_int("GUNICORN_THREADS", 8)


# --------------------------------------------------------------------------
# Timeouts
# --------------------------------------------------------------------------
# `timeout` is not a request deadline. It is a liveness check: the arbiter kills
# a worker that has not touched its heartbeat for this long.
#
# Left at 0 (disabled). Render does not throttle CPU between requests the way
# Cloud Run did, so the original reason for disabling it is gone — but the other
# one stands: Render's proxy already ends a request that runs too long, and two
# timeouts means the shorter one wins silently, in a way that shows up as a
# worker restart rather than as a slow endpoint.
#
# Set it above your slowest request if you want gunicorn to be the one that
# decides. Do not set it *below* one: that turns a slow query into a killed
# worker and a 502.
timeout = env_int("GUNICORN_TIMEOUT", 0)

# How long a worker gets to finish in-flight requests after SIGTERM. It wants to
# be *under* the platform's shutdown grace period, or the difference is
# customers' requests cut off mid-response on every deploy.
#
# **Render allows 30 seconds**, so there is room here that Cloud Run's 10-second
# window did not leave. 25 spends most of it: a checkout that is mid-transaction
# when a deploy lands gets to finish rather than being cut.
#
# It matters more here than the usual "drain behind a load balancer" case,
# because this service mounts a disk — and a disk **disables zero-downtime
# deploys**. Render cannot run the old and new instances side by side over one
# disk, so it stops this one before starting the replacement. There is no
# overlap to fall back on: whatever is in flight when SIGTERM arrives either
# finishes inside this window or is lost.
#
# The push worker threads in `api/push.py` are daemons and are deliberately not
# waited for: losing a notification in flight at deploy time costs a rider one
# poll interval, and a deploy that hangs costs everybody.
graceful_timeout = env_int("GUNICORN_GRACEFUL_TIMEOUT", 25)

# How long an idle keep-alive connection is held open. It should exceed the idle
# timeout of any proxy in front, or the proxy reuses a connection this process
# has just closed and the customer gets a 502 that appears in no application
# log. Render's proxy manages its own pooling, so the default is modest; behind
# a classic load balancer, set this above the balancer's idle timeout.
keepalive = env_int("GUNICORN_KEEPALIVE", 5)


# --------------------------------------------------------------------------
# Preloading
# --------------------------------------------------------------------------
# Import the application once in the master, then fork. Two reasons here:
#
#   - `check_production_safety()` in api/apps.py runs at import. Preloaded, a
#     misconfigured deploy raises once, in the master, and the container exits
#     carrying that message. Without preload each worker fails the same way in a
#     restart loop, and the one line telling you why scrolls past N times a
#     second.
#   - the parsed code is shared copy-on-write instead of duplicated per worker.
#
# The classic hazard is a resource opened at import time and then shared across
# the fork, a database connection being the usual one. Django opens none at
# import — connections are lazy and per-thread — and `ready()` touches only
# settings and the uploads directory, so there is nothing to inherit. Keep it
# that way: a query added to `ready()` would need a `post_fork` hook closing
# connections, and would misbehave confusingly until it got one.
preload_app = env_bool("GUNICORN_PRELOAD", True)

# Recycling a worker after N requests bounds a slow memory leak. Off, because
# with a single worker a recycle is a latency spike on whatever arrives during
# it — possibly a checkout.
#
# **Worth revisiting on Render**, where an instance is long-lived: Cloud Run
# replaced instances often enough that a slow leak never had time to grow, and
# that is no longer doing the work. Turn it on if RSS climbs across a week, and
# set the jitter too, or every worker recycles in lockstep.
max_requests = env_int("GUNICORN_MAX_REQUESTS", 0)
max_requests_jitter = env_int("GUNICORN_MAX_REQUESTS_JITTER", 0)


# --------------------------------------------------------------------------
# Trusting the proxy
# --------------------------------------------------------------------------
# Gunicorn honours X-Forwarded-Proto only from a peer whose address is listed
# here; from anyone else it leaves `wsgi.url_scheme` as http. On Render the peer
# is the platform's proxy, at an address you cannot know in advance, so "*" is
# the only value that works — and it is safe there for exactly the reason
# `SECURE_PROXY_SSL_HEADER` is: the service is not reachable except through that
# proxy.
#
# Read from the *same* variable Django's setting is gated on, deliberately. Two
# layers taking this trust decision from two switches is two chances to set one
# and forget the other, and the failure is quiet in both directions.
forwarded_allow_ips = "*" if env_bool("TRUST_PROXY_SSL_HEADER", True) else "127.0.0.1"


# --------------------------------------------------------------------------
# The control socket
# --------------------------------------------------------------------------
# Gunicorn 25.1 added a unix socket in the arbiter that `gunicornc` connects to
# in order to inspect workers and reload the service by hand. It is on by
# default, and its default path is `$XDG_RUNTIME_DIR/gunicorn.ctl` or — when
# that is unset, which it is on Render — `$HOME/.gunicorn/gunicorn.ctl`.
#
# Render's `$HOME` is a directory the service user cannot create, so the socket
# thread dies at boot with
#
#     ERROR gunicorn.error Control server error: [Errno 13] Permission denied: '/home/edawr'
#
# That line is alarming and harmless: it comes from a daemon thread, the arbiter
# never sees the exception, and the workers serve normally without it. Left
# alone it is a permanent ERROR in the log stream that means nothing, which is
# worse than either fixing it or turning it off.
#
# Turned off, because there is no shell on a Render instance to run `gunicornc`
# from in the first place — a deploy is a push, and a restart is a button.
# Enable it with GUNICORN_CONTROL_SOCKET=true anywhere you do have a shell;
# `control_socket` is set unconditionally so that when you do, the socket lands
# somewhere writable rather than back in an unwritable `$HOME`.
control_socket_disable = not env_bool("GUNICORN_CONTROL_SOCKET", False)
control_socket = env("GUNICORN_CONTROL_SOCKET_PATH", "/tmp/gunicorn.ctl")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
# Gunicorn keeps its own loggers, configured separately from Django's LOGGING.
# Left alone it writes prose, so a production deployment emits the application's
# JSON and the server's plain text to one stream and the aggregator can index
# only half of it. `logconfig_dict` puts both through the same formatter.
_IS_DEVELOPMENT = env("ENVIRONMENT", "development").lower() == "development"

loglevel = env("GUNICORN_LOG_LEVEL", "info")
errorlog = "-"  # stderr

# Access logs, off by default: Render's proxy already records every request with
# its status and latency in the dashboard, so gunicorn's copy says the same thing
# twice into a log stream that is retained and charged for. Turn it on when you
# need a field Render's view does not show — the user agent is the usual one.
_ACCESS_LOG = env_bool("GUNICORN_ACCESS_LOG", False)
accesslog = "-" if _ACCESS_LOG else None

# `accesslog = None` is *not* what switches access logging off once
# `logconfig_dict` is set: gunicorn's `Logger.access()` asks whether any logging
# configuration exists at all, and this dict counts as one. The null handler
# below is what actually silences it.
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"format": "{levelname:8} {name} {message}", "style": "{"},
        "json": {"()": "config.logformat.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "console" if _IS_DEVELOPMENT else "json",
        },
        "null": {"class": "logging.NullHandler"},
    },
    "loggers": {
        "gunicorn.error": {
            "handlers": ["console"],
            "level": loglevel.upper(),
            "propagate": False,
        },
        "gunicorn.access": {
            "handlers": ["console" if _ACCESS_LOG else "null"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
