# Container image for the eDawr API, built for Render's Docker runtime.
#
# **This file exists because a service's runtime cannot be changed after it is
# created**, except through Render's API or a Blueprint sync — not in the
# dashboard. `edawr-api` was created by hand while an earlier Dockerfile still
# existed, so Render fixed its runtime as Docker, and every deploy since has
# failed with:
#
#     error: failed to solve: failed to read dockerfile: open Dockerfile: no such file or directory
#
# `render.yaml` still says `runtime: python`, and that remains the intended
# deployment — see deployment.md, "The service is not the one render.yaml
# describes". A Blueprint sync switches the service to the native runtime and
# ignores this file entirely. Until then, this is what that service builds.
#
# Build and run it exactly as Render will:
#   docker build -t edawr-api .
#   docker run --rm -p 10000:10000 --env-file .env -e PORT=10000 edawr-api

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
# and Render shows you an empty log until the buffer happens to flush —
# including, memorably, when the process is dying and you most want the log.
# PYTHONDONTWRITEBYTECODE: the bytecode is already compiled into the venv.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Two packages, each earning its place:
#
# `postgresql-client-17` restores `manage.py backup_database`, which the native
# runtime cannot run at all — no apt, no pg_dump. Two things make this more than
# an apt line:
#
#   1. **The version has to be at least the server's.** pg_dump refuses to dump
#      a newer server than itself — it cannot know what syntax it has not been
#      taught. Debian bookworm ships postgresql-client-15 and Neon runs 17, so
#      the stock package would fail at the first backup with a version error
#      rather than at build time. Hence the PGDG repository below.
#   2. **Keep this in step with Neon.** If the managed Postgres is upgraded to
#      18, this number goes up with it. The failure is loud (backup_database
#      surfaces pg_dump's stderr verbatim) but it happens at 2am on the day you
#      needed the backup, so it is worth a note here.
#
# `gosu` is what lets the entrypoint start as root, fix the ownership of a disk
# Render mounted after this image was built, and then drop to an unprivileged
# uid before exec'ing gunicorn. Debian ships it; the alternative — assuming
# `setpriv` or `su` behaves — is a guess this deploy cannot afford.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg gosu; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-17; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*

# A fixed, unprivileged uid. The process that serves the store never needs root,
# and the entrypoint below gives it up before gunicorn starts.
RUN groupadd --gid 10001 edawr \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin edawr

WORKDIR /app

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 manage.py ./
COPY --chown=10001:10001 config/ ./config/
COPY --chown=10001:10001 api/ ./api/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Created so the image runs without a disk attached — a local `docker run`, and
# the first deploy before the disk exists. When Render mounts one at
# /var/data, UPLOAD_DIR points inside it and the entrypoint takes ownership of
# that path instead. `backups/` is separate on purpose: SERVE_MEDIA=true makes
# Django serve everything under the upload directory to the public internet,
# which a database dump must never be.
RUN mkdir -p /app/uploads /app/backups \
 && chown 10001:10001 /app/uploads /app/backups

# Deliberately **not** `USER 10001`. A disk is mounted after the build, with an
# ownership this image does not choose, so something with root has to chown the
# mount point before the first upload is written. The entrypoint does exactly
# that and nothing else, then hands off with gosu — so gunicorn, and every
# thread serving a request, still runs as 10001.

# Documentation only — Render injects $PORT (10000 by default) and
# config/gunicorn.py binds it.
EXPOSE 10000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Every server setting — the worker model, the timeouts, what is trusted from
# the proxy, how the server's own logs are formatted — lives in
# config/gunicorn.py, where each one can carry the reason it holds that value.
#
# The exec form (a JSON array, no shell) matters: the entrypoint `exec`s this,
# so gunicorn becomes PID 1 and receives Render's SIGTERM directly. A shell
# holding PID 1 ignores the signal, and every deploy then waits out the full
# 30-second grace period before being killed — with in-flight checkouts cut off,
# because a mounted disk means there is no second instance to fall back to.
CMD ["gunicorn", "--config", "config/gunicorn.py"]
