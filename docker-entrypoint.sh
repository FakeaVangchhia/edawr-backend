#!/bin/sh
# Runs as root, does the two things root is needed for, then stops being root.
#
# Everything here is deliberately small. An entrypoint runs before the
# application can log anything, so a failure in it is a container that exits
# with no explanation in the dashboard — which is the state this service has
# already been in for three days.
set -eu

# --------------------------------------------------------------------------
# 1. The disk
# --------------------------------------------------------------------------
# Render mounts a disk after the image is built, and the mount point's
# ownership is not something the Dockerfile chose — so a container that dropped
# to uid 10001 at build time can find itself unable to write to the one
# directory it exists to write to. The symptom is not a boot failure: Django
# starts, the store serves, and the *first product image upload* 500s.
#
# `|| true` because this is not worth failing a boot over. If the chown is
# refused, api/apps.py already prints a warning naming the directory, and
# api/views/uploads.py surfaces the real error at the point of use.
UPLOADS="${UPLOAD_DIR:-/app/uploads}"
mkdir -p "$UPLOADS" 2>/dev/null || true
chown -R 10001:10001 "$UPLOADS" 2>/dev/null || true

# --------------------------------------------------------------------------
# 2. Migrations, only if asked
# --------------------------------------------------------------------------
# `render.yaml` runs `manage.py migrate` as a preDeployCommand, on a separate
# instance, before the new build takes traffic — which is the right place for
# it, because a failed migration then aborts the deploy while the old version
# keeps serving. A service created by hand has no such command unless somebody
# set one.
#
# So this is opt-in, off by default: set RUN_MIGRATIONS=true only if the
# service has no pre-deploy command. It is safe here *because a mounted disk
# pins this service to a single instance* — there is no second container racing
# to apply the same migration. Remove the variable the moment a pre-deploy
# command exists, or the schema change runs twice on every deploy.
#
# Not guarded with `|| true`: a container that serves requests against a schema
# it failed to migrate returns 500s that look like application bugs. Failing
# here is loud, and the health check keeps the old version in place.
if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    echo "entrypoint: RUN_MIGRATIONS=true — applying migrations"
    gosu 10001:10001 python manage.py migrate --noinput
fi

# --------------------------------------------------------------------------
# 3. Hand over
# --------------------------------------------------------------------------
# `exec` replaces this shell, so gunicorn is PID 1 and gets SIGTERM directly at
# deploy time rather than through a shell that ignores it.
exec gosu 10001:10001 "$@"
