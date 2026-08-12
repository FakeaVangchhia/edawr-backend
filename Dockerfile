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
# or the upload view cannot write to it. See docs/deployment.md.
RUN groupadd --gid 10001 edawr \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin edawr

WORKDIR /app

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 manage.py ./
COPY --chown=10001:10001 config/ ./config/
COPY --chown=10001:10001 api/ ./api/

# Created so the image runs without a volume attached (local `docker run`, and
# any smoke test before the bucket exists). In Cloud Run the Cloud Storage
# volume mounts over this directory.
RUN mkdir -p /app/uploads && chown 10001:10001 /app/uploads

USER 10001:10001

# Documentation only — Cloud Run injects $PORT and ignores EXPOSE.
EXPOSE 8080

# `sh -c` because $PORT must be expanded at runtime; the exec form would pass
# the literal string "$PORT" to gunicorn. `exec` replaces the shell so gunicorn
# becomes PID 1 and receives Cloud Run's SIGTERM directly — without it the
# shell holds PID 1, ignores the signal, and every deploy waits out the full
# 10-second grace period before being killed.
#
#   --workers 1 --threads 8   One process, eight threads. These requests are
#                             IO-bound (Postgres and Redis are both network
#                             hops), so threads are the resource that matters
#                             and a second worker would only duplicate the
#                             ~80 MB interpreter inside a 512 MiB instance.
#   --timeout 0               Disables gunicorn's own worker timeout and lets
#                             Cloud Run's request timeout be the only one. Two
#                             timeouts means the shorter one wins silently.
CMD exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 1 \
    --threads 8 \
    --timeout 0
