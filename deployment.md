# Deploying the eDawr API to Google Cloud Run

The API only. The storefront, console and rider app deploy separately.

**On the `runserver` warning:** it is correct and there is nothing to fix.
Production serves with gunicorn via `config/gunicorn.py`, which is what the
Dockerfile runs. Keep using `runserver` locally. (gunicorn is POSIX-only; on
Windows, `uv run waitress-serve --listen=127.0.0.1:8000 config.wsgi:application`
runs the app without the auto-reloader.)

## Before you start

```bash
gcloud auth login
export PROJECT_ID=your-project-id
export REGION=asia-south1        # closest to Aizawl
export SERVICE=edawr-api
export REPO=edawr

gcloud config set project $PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
```

Docker is not needed locally — Cloud Build builds the image.

**Postgres and Redis are both mandatory**; the app refuses to boot without them
(`check_production_safety()` in `api/apps.py`). Postgres you already have on
Neon and nothing changes for Cloud Run. Redis only holds throttle counters, so
a serverless `rediss://` provider is enough — Memorystore works too, but needs
Direct VPC egress.

## 1. Secrets

```bash
# Two different random values — one secret signing two things is one leak.
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"

for name in jwt-secret django-secret-key database-url cache-url; do
  gcloud secrets create $name --replication-policy=automatic
done

# printf, not echo — a trailing newline in a database URL breaks the connection.
printf '%s' 'JWT_SECRET_VALUE'     | gcloud secrets versions add jwt-secret --data-file=-
printf '%s' 'DJANGO_SECRET_VALUE'  | gcloud secrets versions add django-secret-key --data-file=-
printf '%s' 'postgresql://...'     | gcloud secrets versions add database-url --data-file=-
printf '%s' 'rediss://...'         | gcloud secrets versions add cache-url --data-file=-
```

## 2. Buckets and service account

Two buckets, never one: production serves everything under `uploads/` publicly,
and a database dump there would be a public download of every customer's name,
phone and address.

```bash
gcloud storage buckets create gs://${PROJECT_ID}-uploads --location=$REGION
gcloud storage buckets create gs://${PROJECT_ID}-backups --location=$REGION

gcloud iam service-accounts create edawr-api
export SA=edawr-api@${PROJECT_ID}.iam.gserviceaccount.com

for name in jwt-secret django-secret-key database-url cache-url; do
  gcloud secrets add-iam-policy-binding $name \
    --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
done

for b in uploads backups; do
  gcloud storage buckets add-iam-policy-binding gs://${PROJECT_ID}-${b} \
    --member=serviceAccount:$SA --role=roles/storage.objectUser
done
```

Neither bucket is public. Django serves the images off the mounted volume, so
the browser never talks to Cloud Storage.

## 3. Build

```bash
gcloud artifacts repositories create $REPO \
  --repository-format=docker --location=$REGION

export IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}
export TAG=$(git rev-parse --short HEAD)

gcloud builds submit --tag ${IMAGE}:${TAG} .      # run from backend/
```

Tag with the commit, not `latest` — the migration job must run the same image.

## 4. Deploy

```bash
gcloud run deploy $SERVICE \
  --image=${IMAGE}:${TAG} \
  --region=$REGION \
  --service-account=$SA \
  --allow-unauthenticated \
  --min-instances=1 --max-instances=4 \
  --cpu=1 --memory=512Mi --timeout=60 \
  --set-secrets="JWT_SECRET=jwt-secret:latest,DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest,CACHE_URL=cache-url:latest" \
  --set-env-vars="^|^ENVIRONMENT=production|ALLOWED_HOSTS=PLACEHOLDER|CORS_ORIGINS=https://edawr.example,https://admin.edawr.example|SERVE_MEDIA=true|SERVE_API_DOCS=false|STORE_TIMEZONE=Asia/Kolkata" \
  --add-volume mount-path=/app/uploads,type=cloud-storage,bucket=${PROJECT_ID}-uploads,mount-options="uid=10001;gid=10001" \
  --add-volume mount-path=/app/backups,type=cloud-storage,bucket=${PROJECT_ID}-backups,mount-options="uid=10001;gid=10001"
```

Then set the real hostname, which you only know once the service exists:

```bash
export API_HOST=$(gcloud run services describe $SERVICE --region=$REGION \
  --format='value(status.url)' | sed 's#https://##')

gcloud run services update $SERVICE --region=$REGION \
  --update-env-vars="ALLOWED_HOSTS=${API_HOST}"
```

Three things there fail quietly if changed:

- `^|^` switches the env-var separator to `|`, because `CORS_ORIGINS` contains
  commas. Without it the origins become separate variables.
- `uid=10001;gid=10001` matches the container's user. Omit it and uploads fail
  with a permission error. The separator is a semicolon.
- Jobs take different volume flags: `--add-volume name=…` plus a separate
  `--add-volume-mount`.

## 5. Migrations

Run as a job on the same image tag, never at container start-up.

```bash
gcloud run jobs deploy ${SERVICE}-migrate \
  --image=${IMAGE}:${TAG} --region=$REGION --service-account=$SA \
  --command=python --args=manage.py,migrate \
  --set-secrets="JWT_SECRET=jwt-secret:latest,DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest,CACHE_URL=cache-url:latest" \
  --set-env-vars="^|^ENVIRONMENT=production|ALLOWED_HOSTS=${API_HOST}|CORS_ORIGINS=https://edawr.example"

gcloud run jobs execute ${SERVICE}-migrate --region=$REGION --wait
```

Every later deploy: build → update the job's image → execute the job → deploy
the service.

## 6. First admin

Pass the password at execution time so it is not stored in the job config.

```bash
gcloud run jobs deploy ${SERVICE}-admin \
  --image=${IMAGE}:${TAG} --region=$REGION --service-account=$SA \
  --command=python --args=manage.py,check \
  --set-secrets="<same as migrate>" --set-env-vars="<same as migrate>"

read -rsp 'New admin password: ' ADMIN_PW; echo
gcloud run jobs execute ${SERVICE}-admin --region=$REGION --wait \
  --args="^|^manage.py|seed_admin|--email|you@example.com|--password|${ADMIN_PW}|--role|admin"
```

**Never run `manage.py seed` in production** — it deletes every row.

## 7. Verify

```bash
curl -s https://${API_HOST}/api/health          # {"status":"ok"}
curl -s https://${API_HOST}/api/health/ready    # checks database and cache
curl -s https://${API_HOST}/api/store/config
```

## 8. Wire up the clients

| Set on | Variable | Value |
|---|---|---|
| Cloud Run | `CORS_ORIGINS` | the storefront and console origins |
| Cloud Run | `ALLOWED_HOSTS` | the API's hostname(s) |
| All three clients | `NEXT_PUBLIC_API_URL` | `https://${API_HOST}` |

Custom domain:

```bash
gcloud run domain-mappings create --service=$SERVICE \
  --domain=api.edawr.example --region=$REGION
```

Add the new hostname to `ALLOWED_HOSTS` afterwards. If you later put a CDN or
load balancer in front, raise `NUM_PROXIES` to match the hop count — it
defaults to 1, which is correct for Cloud Run alone.

## Operations

```bash
# Logs
gcloud run services logs tail $SERVICE --region=$REGION

# Roll back (code only — not the schema)
gcloud run revisions list --service=$SERVICE --region=$REGION
gcloud run services update-traffic $SERVICE --region=$REGION --to-revisions=REVISION=100
```

Backups: deploy `manage.py backup_database` as a job with the backups volume
mounted and `BACKUP_DIR=/app/backups`, then trigger it from Cloud Scheduler at
`https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${SERVICE}-backup:run`.
The scheduler's service account needs `roles/run.invoker` on the job.

To pause the store, do not redeploy — opening hours and the accept-orders
switch live in the `store_settings` table, editable from the console.

**Pruning location history is not optional.** `order_location_pings` is
append-only and grows with every delivery; unpruned it becomes a permanent
record of where your customers live. Nothing else deletes it. Deploy
`manage.py prune_locations` as a job (no bucket mount — it only deletes rows)
and schedule it nightly, after the backup:

```bash
gcloud run jobs deploy ${SERVICE}-prune-locations \
  --image=${IMAGE}:${TAG} --region=$REGION --service-account=$SA \
  --command=python --args=manage.py,prune_locations \
  --set-secrets="<same as migrate>" --set-env-vars="<same as migrate>"

gcloud scheduler jobs create http ${SERVICE}-prune-nightly \
  --location=$REGION --schedule="0 3 * * *" --time-zone="Asia/Kolkata" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${SERVICE}-prune-locations:run" \
  --http-method=POST --oauth-service-account-email=$SA

gcloud run jobs add-iam-policy-binding ${SERVICE}-prune-locations --region=$REGION \
  --member=serviceAccount:$SA --role=roles/run.invoker
```

A non-zero "customer positions on ended orders" count in its output is worth
looking at: `advance_status` deletes those the moment an order ends, so a row
reaching the sweep means something moved an order to a terminal status without
going through the state machine.


## Troubleshooting

| Symptom | Cause |
|---|---|
| Container exits at start listing problems | `check_production_safety()` did its job; the log names each one |
| Every request 400s | `ALLOWED_HOSTS` missing the hostname in use |
| Storefront empty, no CORS error | `NEXT_PUBLIC_API_URL` wrong — the CSP is blocking the API |
| Storefront empty, CORS error | `CORS_ORIGINS` missing the storefront origin |
| Upload permission error | `mount-options=uid=10001;gid=10001` missing |
| Uploaded images 404 | `SERVE_MEDIA` is not `true` |
| Rate limits appear absent | `NUM_PROXIES` too high |
| Requests cut off on deploy | `GUNICORN_GRACEFUL_TIMEOUT` above Cloud Run's 10s grace |
| `pg_dump: server version mismatch` | Neon upgraded; bump `postgresql-client-17` in the Dockerfile |
| Tracking page never shows the rider | Order is not `Dispatched`, no rider assigned, or the last fix is older than `LOCATION_STALE_SECONDS` (90s) |
