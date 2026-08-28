# Deploying the eDawr API to Google Cloud Run

The API only — the storefront, console and rider app deploy separately.

**On the `runserver` warning:** it is correct and there is nothing to fix.
Production serves with gunicorn via `config/gunicorn.py`, which is what the
Dockerfile runs. Keep using `runserver` locally. (gunicorn is POSIX-only; on
Windows use `uv run waitress-serve --listen=127.0.0.1:8000 config.wsgi:application`.)

**Postgres and Redis are both mandatory** — `check_production_safety()` in
`api/apps.py` refuses to boot without them. Postgres you have on Neon. Redis
only holds throttle counters, so any serverless `rediss://` provider is enough.

## 1. One-time setup

Everything here is done once, ever. Later deploys start at step 2.

```bash
gcloud auth login
export PROJECT_ID=edawr-506816
export REGION=asia-south1        # closest to Aizawl
export SERVICE=edawr-api
export REPO=edawr

gcloud config set project $PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

# --- secrets. Generate two DIFFERENT random values: one secret signing two
#     things is one leak. printf, not echo — a trailing newline in a database
#     URL breaks the connection.
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"

for name in jwt-secret django-secret-key database-url cache-url; do
  gcloud secrets create $name --replication-policy=automatic
done
printf '%s' 'JWT_SECRET_VALUE'    | gcloud secrets versions add jwt-secret --data-file=-
printf '%s' 'DJANGO_SECRET_VALUE' | gcloud secrets versions add django-secret-key --data-file=-
printf '%s' 'postgresql://...'    | gcloud secrets versions add database-url --data-file=-
printf '%s' 'rediss://...'        | gcloud secrets versions add cache-url --data-file=-

# --- two buckets, never one. Production serves everything under uploads/
#     publicly; a database dump there would be a public download of every
#     customer's name, phone and address. Neither bucket is itself public —
#     Django serves images off the mounted volume.
gcloud storage buckets create gs://${PROJECT_ID}-uploads --location=$REGION
gcloud storage buckets create gs://${PROJECT_ID}-backups --location=$REGION

# --- service account
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

# --- image registry
gcloud artifacts repositories create $REPO \
  --repository-format=docker --location=$REGION
```

## 2. Deploy

Two shell variables carry the config every command below needs, so the secret
list and the env list are written once rather than five times.

```bash
export IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}
export TAG=$(git rev-parse --short HEAD)

# `^|^` switches the env-var separator to `|`, because CORS_ORIGINS contains
# commas. Without it the origins become separate variables.
export SECRETS="--set-secrets=JWT_SECRET=jwt-secret:latest,DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest,CACHE_URL=cache-url:latest"
export ENVS="--set-env-vars=^|^ENVIRONMENT=production|ALLOWED_HOSTS=PLACEHOLDER|CORS_ORIGINS=https://edawr.example,https://admin.edawr.example|SERVE_MEDIA=true|SERVE_API_DOCS=false|STORE_TIMEZONE=Asia/Kolkata|BACKUP_DIR=/app/backups"

# Tag with the commit, not `latest` — the job below must run the same image.
gcloud builds submit --tag ${IMAGE}:${TAG} .      # run from backend/
```

**One job, reused for every management command.** Deploy it once; override
`--args` at execution time. `--task-timeout` is generous because a backup on a
large database is not fast.

```bash
gcloud run jobs deploy ${SERVICE}-task \
  --image=${IMAGE}:${TAG} --region=$REGION --service-account=$SA \
  --command=python --args=manage.py,check --task-timeout=900 \
  "$SECRETS" "$ENVS" \
  --add-volume name=backups,type=cloud-storage,bucket=${PROJECT_ID}-backups \
  --add-volume-mount volume=backups,mount-path=/app/backups

# Migrations run as a job, never at container start-up.
gcloud run jobs execute ${SERVICE}-task --region=$REGION --wait \
  --args=manage.py,migrate
```

Then the service itself:

```bash
gcloud run deploy $SERVICE \
  --image=${IMAGE}:${TAG} --region=$REGION --service-account=$SA \
  --allow-unauthenticated \
  --min-instances=1 --max-instances=4 \
  --cpu=1 --memory=512Mi --timeout=60 \
  "$SECRETS" "$ENVS" \
  --add-volume mount-path=/app/uploads,type=cloud-storage,bucket=${PROJECT_ID}-uploads,mount-options="uid=10001;gid=10001" \
  --add-volume mount-path=/app/backups,type=cloud-storage,bucket=${PROJECT_ID}-backups,mount-options="uid=10001;gid=10001"

# The hostname only exists once the service does.
export API_HOST=$(gcloud run services describe $SERVICE --region=$REGION \
  --format='value(status.url)' | sed 's#https://##')
gcloud run services update $SERVICE --region=$REGION \
  --update-env-vars="ALLOWED_HOSTS=${API_HOST}"
```

Note that services and jobs take **different volume flags** — services one
`--add-volume mount-path=…`, jobs `--add-volume name=…` plus a separate
`--add-volume-mount`. Both spellings appear above; they are not interchangeable.

`--min-instances=1` is not about throughput. Cloud Run throttles CPU to near
zero between requests, and `api/push.py` sends notifications on a thread after
the response — with no warm instance a rider's phone buzzes late or not at all.
It also avoids a cold start on the day's first order.

## 3. First admin

The password is passed at execution time so it is never stored in the job config.

```bash
read -rsp 'New admin password: ' ADMIN_PW; echo
gcloud run jobs execute ${SERVICE}-task --region=$REGION --wait \
  --args="^|^manage.py|seed_admin|--email|you@example.com|--password|${ADMIN_PW}|--role|admin"
```

**Never run `manage.py seed` in production** — it deletes every row.

## 4. Verify

```bash
curl -s https://${API_HOST}/api/health          # {"status":"ok"}
curl -s https://${API_HOST}/api/health/ready    # checks database and cache
curl -s https://${API_HOST}/api/store/config
```

## 5. Continuous deployment

Cloud Run → your service → **Set up continuous deployment**. Authorise GitHub,
pick `edawr-backend`, branch `^master$`, build type **Dockerfile**. That creates
a Cloud Build trigger; from then on a push to master builds and deploys itself,
and steps 2's build/deploy commands are only needed for a manual rollout.

**This only replaces the image.** Secrets, env vars, volume mounts and
`--min-instances` stay exactly as you configured them — which is what makes it
safe to hand the deploy to a git push.

**The one rule: a push containing a migration deploys code before the schema
exists.** Auto-deploy has no idea a migration is in the commit. So when the
commit adds one, run the job *first*, against the image already live, then push:

```bash
gcloud run jobs execute ${SERVICE}-task --region=$REGION --wait --args=manage.py,migrate
```

Order only truly matters for a migration that drops or renames something. One
that just adds tables or nullable columns (`0012_live_location`) is safe either
way — the old code ignores what it does not know about.

## 6. Wire up the clients

| Set on | Variable | Value |
|---|---|---|
| Cloud Run | `CORS_ORIGINS` | the storefront and console origins |
| Cloud Run | `ALLOWED_HOSTS` | the API's hostname(s) |
| All three clients | `NEXT_PUBLIC_API_URL` | `https://${API_HOST}` |

```bash
gcloud run domain-mappings create --service=$SERVICE \
  --domain=api.edawr.example --region=$REGION
```

Add the new hostname to `ALLOWED_HOSTS` afterwards. If you later put a CDN or
load balancer in front, raise `NUM_PROXIES` to match the hop count — it defaults
to 1, which is correct for Cloud Run alone.

## Scheduled jobs

Both reuse `${SERVICE}-task`; only the args and the schedule differ.

**Pruning location history is not optional.** `order_location_pings` is
append-only and grows with every delivery; unpruned it becomes a permanent
record of where your customers live. Nothing else deletes it.

```bash
# Grant the scheduler permission to start the job, once.
gcloud run jobs add-iam-policy-binding ${SERVICE}-task --region=$REGION \
  --member=serviceAccount:$SA --role=roles/run.invoker

export JOB_URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${SERVICE}-task:run"

# Backup at 02:00, prune at 03:00 — so a restored dump is already pruned.
gcloud scheduler jobs create http ${SERVICE}-backup-nightly \
  --location=$REGION --schedule="0 2 * * *" --time-zone="Asia/Kolkata" \
  --uri="$JOB_URI" --http-method=POST --oauth-service-account-email=$SA \
  --headers="Content-Type=application/json" \
  --message-body='{"overrides":{"containerOverrides":[{"args":["manage.py","backup_database"]}]}}'

gcloud scheduler jobs create http ${SERVICE}-prune-nightly \
  --location=$REGION --schedule="0 3 * * *" --time-zone="Asia/Kolkata" \
  --uri="$JOB_URI" --http-method=POST --oauth-service-account-email=$SA \
  --headers="Content-Type=application/json" \
  --message-body='{"overrides":{"containerOverrides":[{"args":["manage.py","prune_locations"]}]}}'
```

A non-zero "customer positions on ended orders" count in the prune output is
worth looking at: `advance_status` deletes those the moment an order ends, so a
row reaching the sweep means something moved an order to a terminal status
without going through the state machine.

## Operations

```bash
gcloud run services logs tail $SERVICE --region=$REGION

# Roll back — code only, never the schema.
gcloud run revisions list --service=$SERVICE --region=$REGION
gcloud run services update-traffic $SERVICE --region=$REGION --to-revisions=REVISION=100
```

To pause the store, **do not redeploy**. Opening hours and the accept-orders
switch live in the `store_settings` table, editable from the console.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Container exits at start listing problems | `check_production_safety()` did its job; the log names each one |
| Every request 400s | `ALLOWED_HOSTS` missing the hostname in use |
| Storefront empty, no CORS error | `NEXT_PUBLIC_API_URL` wrong — the CSP is blocking the API |
| Storefront empty, CORS error | `CORS_ORIGINS` missing the storefront origin |
| Origins arrive as separate env vars | `^\|^` dropped from `$ENVS`; commas in `CORS_ORIGINS` split |
| Upload permission error | `mount-options=uid=10001;gid=10001` missing |
| Uploaded images 404 | `SERVE_MEDIA` is not `true` |
| Rate limits appear absent | `NUM_PROXIES` too high |
| Requests cut off on deploy | `GUNICORN_GRACEFUL_TIMEOUT` above Cloud Run's 10s grace |
| `pg_dump: server version mismatch` | Neon upgraded; bump `postgresql-client-17` in the Dockerfile |
| Push notifications arrive late or never | `--min-instances` is 0; the sending thread is CPU-throttled between requests |
| Tracking page never shows the rider | Order is not `Dispatched`, no rider assigned, or the last fix is older than `LOCATION_STALE_SECONDS` (90s) |
