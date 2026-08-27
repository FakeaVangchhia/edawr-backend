# Container image for the eDawr API.
#
# Two stages: the first resolves dependencies with uv, the second copies only
# the resulting virtualenv into a clean runtime. uv itself, the lockfile and the
# build cache never reach the shipped image.
#
# Build and run locally exactly as Cloud Run will:
#   docker build -t edawr-api .
#   docker run --rm -p 8080:8080 --env-file .env -e PORT=8080 edawr-api

# --------------------------------------------------------------------------
# Stage 1 — dependencies
# --------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

# Copy the dependency tree into the venv rather than hardlinking it out of the
# cache, because the next stage copies the venv to a different filesystem.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Only the two dependency files, so this layer is rebuilt when dependencies
# change rather than every time a .py file does.
COPY pyproject.toml uv.lock ./

# --frozen fails if uv.lock disagrees with pyproject.toml instead of silently
# re-resolving — a deploy is the wrong moment to discover you are shipping
# versions nobody has run. --no-dev omits dev-only dependencies.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --------------------------------------------------------------------------
# Stage 2 — runtime
# --------------------------------------------------------------------------
# Must stay the same Python version and Debian release as the builder above.
# The virtualenv copied out of that stage is not relocatable: its bin/ symlinks
# point at an absolute interpreter path, and the uv image is literally this
# image with uv added, so the path matches. Bump one of these two lines without
# the other and the venv's python symlink dangles — which surfaces as "exec
# format error" or a missing interpreter, not as anything mentioning versions.
FROM python:3.14-slim-bookworm

# PYTHONUNBUFFERED: without it Python block-buffers stdout when it is a pipe,
# and Cloud Run shows you an empty log until the buffer happens to flush —
# including, memorably, when the process is dying and you most want the log.
# PYTHONDONTWRITEBYTECODE: the bytecode is already compiled into the venv.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# A fixed, unprivileged uid. The number matters beyond this file: the Cloud
# Storage volume holding /app/uploads must be mounted with uid=10001,gid=10001
# or the upload view cannot write to it. See deployment.md.
# pg_dump, for `manage.py backup_database`. Two things make this more than an
# apt line:
#
#   1. **The version has to be at least the server's.** pg_dump refuses to dump
#      a newer server than itself — it cannot know what syntax it has not been
#      taught. Debian bookworm ships postgresql-client-15, and Neon runs 17, so
#      the stock package would fail at the first backup with a version error
#      rather than at build time. Hence the PGDG repository below.
#   2. **Keep this in step with Neon.** If the managed Postgres is upgraded to
#      18, this number goes up with it. The failure is loud (backup_database
#      surfaces pg_dump's stderr verbatim) but it happens at 2am on the day you
#      needed the backup, so it is worth a note here.
#
# --no-install-recommends and the apt list cleanup keep this to ~25 MB, which is
# the price of the one thing that turns a bad day into a recoverable one.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-17; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 edawr \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin edawr

WORKDIR /app

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 manage.py ./
COPY --chown=10001:10001 config/ ./config/
COPY --chown=10001:10001 api/ ./api/

# Created so the image runs without a volume attached (local `docker run`, and
# any smoke test before the bucket exists). In Cloud Run each is a separate
# Cloud Storage volume mounted over these directories — and they are separate
# buckets on purpose: SERVE_MEDIA=true makes Django serve everything under
# uploads/ to the public internet, which a database dump must never be.
RUN mkdir -p /app/uploads /app/backups \
 && chown 10001:10001 /app/uploads /app/backups

USER 10001:10001

# Documentation only — Cloud Run injects $PORT and ignores EXPOSE.
EXPOSE 8080

# Every server setting — the worker model, the timeouts, what is trusted from
# the proxy, how the server's own logs are formatted — lives in
# config/gunicorn.py, where each one can carry the reason it holds that value.
# What was a row of flags here is now a file that explains itself, and the
# settings that used to be baked into this line are environment variables with
# the same defaults.
#
# The exec form (a JSON array, no shell) is what makes gunicorn PID 1 and gives
# it Cloud Run's SIGTERM directly. A shell holding PID 1 ignores the signal, and
# every deploy then waits out the full 10-second grace period before being
# killed. This used to need `sh -c` so that $PORT was expanded at runtime;
# config/gunicorn.py reads PORT from the environment itself, so the shell is no
# longer needed for that either.
CMD ["gunicorn", "--config", "config/gunicorn.py"]
