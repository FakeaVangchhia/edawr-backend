# Deploying the eDawr API to Google Cloud Run

One document, one service. This is the API (`edawr-backend`) only — the
storefront, the staff console and the rider app deploy separately and are not
covered here beyond the two settings that have to agree with this deploy
(`CORS_ORIGINS`, and each client's `NEXT_PUBLIC_API_URL`).

---

## 0. The thing you are actually fixing

If you arrived here from this line:

```
WARNING: This is a development server. Do not use it in a production setting.
Use a production WSGI or ASGI server instead.
```

that is `manage.py runserver` telling you the truth about itself. It is a
development tool: one process, an auto-reloader, and no interest in
concurrency. Nothing about your machine is wrong.

**The production server already exists in this repository.** It is gunicorn,
configured in `config/gunicorn.py`, and the Dockerfile's last line starts it:

```dockerfile
CMD ["gunicorn", "--config", "config/gunicorn.py"]
```

So the fix is not a code change — it is *deploying the container* rather than
running `runserver`. That is what the rest of this file is.

Two notes before you go on:

- **gunicorn will not run on Windows.** It needs `fork` and `fcntl`. This is
  not a problem to solve; the container is Linux and Cloud Run runs the
  container. Locally you keep using `runserver`, warning and all.
- **To check the server behaviour locally on Windows**, `waitress` is in the
  dev dependency group:

  ```bash
  uv run waitress-serve --listen=127.0.0.1:8000 --threads=8 config.wsgi:application
  ```

  That gives you the application with no auto-reloader. It is a local check
  only — it is never deployed, and it reads none of `config/gunicorn.py`. To
  exercise *that* file you need the container (Step 6).

---

## 1. What gets deployed, and what it depends on

```
                    ┌──────────────────────────┐
  storefront  ───►  │  Cloud Run service       │  ───►  Neon Postgres
  console     ───►  │  edawr-api               │  ───►  Redis (throttle counters)
  rider app   ───►  │  gunicorn, gthread       │  ───►  Expo push (optional)
                    │  1 worker × 8 threads    │
                    └───────────┬──────────────┘
                                │  volume mounts
                    ┌───────────┴──────────────┐
                    │ gs://…-uploads           │  product images, served by Django
                    │ gs://…-backups           │  pg_dump archives, never served
                    └──────────────────────────┘
```

Four external dependencies. Two of them the app **refuses to boot without**:

| Dependency | Required? | Why the app insists |
|---|---|---|
| Postgres | **Yes** | `select_for_update()` in `api/checkout.py` is a no-op on SQLite — two customers can buy the same last unit of stock. And a container filesystem is wiped every deploy. |
| Redis | **Yes** | DRF throttle counters. In per-process memory every rate limit is multiplied by the worker count, and the login limit is the only thing making a 4-digit rider PIN a credential. |
| Uploads bucket | No (but) | Without it, product images are written to the container filesystem and vanish on the next deploy. |
| Backups bucket | No (but) | `manage.py backup_database` has nowhere durable to write. |

`api/apps.py::check_production_safety()` enforces the first two, plus
`JWT_SECRET`, `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS` and `CORS_ORIGINS`. It
raises at import, and because `config/gunicorn.py` sets `preload_app = True`
it raises **once, in the master, with the reason in the log**, and the
container exits. A misconfigured deploy fails loudly rather than serving.

### Prerequisites on your machine

```bash
gcloud --version      # Google Cloud CLI
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

You do **not** need Docker locally — Cloud Build builds the image server-side
(Step 6). Install Docker only if you want to run the container on your own
machine first.

Set these once so the commands below can be pasted as-is:

```bash
export PROJECT_ID=your-project-id
export REGION=asia-south1          # Mumbai — closest region to Aizawl
export SERVICE=edawr-api
export REPO=edawr
```

`asia-south1` matters more than it looks: every request from Aizawl pays the
round trip, and the 15-minute promise is measured from a phone on Indian mobile
data.

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com
```

---

## 2. Postgres

You are already on **Neon** (`DATABASE_URL` in your `.env` points at
`…neon.tech`, on the pooler endpoint). Nothing needs to change for Cloud Run —
Neon is reachable over the public internet with TLS, so there is no VPC
connector, no Cloud SQL proxy and no private networking to configure.

Two things to confirm rather than assume:

1. **Use the pooler endpoint** (`-pooler` in the hostname), which you already
   are. Cloud Run scales to many instances and each holds up to
   `workers × threads` connections; the pooler is what stops that becoming
   Neon's connection limit. See the connection arithmetic in Step 9.
2. **Keep `?sslmode=require`** in the URL. `dj_database_url` passes the query
   string through to psycopg.

You do not need to disable prepared statements. Django 6.1 sets
`prepare_threshold` to `None` by default for psycopg3, so nothing conflicts
with the pooler's transaction mode — this is the classic PgBouncer failure and
this stack does not have it.

**If you would rather use Cloud SQL**, deploy with
`--add-cloudsql-instances=PROJECT:REGION:INSTANCE` and point `DATABASE_URL` at
the unix socket:

```
postgres://USER:PASSWORD@/edawr?host=/cloudsql/PROJECT:REGION:INSTANCE
```

The service account then needs `roles/cloudsql.client`. Everything else in
this document is unchanged.

---

## 3. Redis

The app will not boot without `CACHE_URL`. Two ways to supply it, and for a
single store the first is the right one.

### Option A — a serverless Redis over TLS (recommended)

Upstash, Redis Cloud's free tier, or similar. You get a `rediss://` URL that
works from Cloud Run with no networking setup, which is the same trade you
already made by choosing Neon over Cloud SQL.

```
CACHE_URL=rediss://default:PASSWORD@host.upstash.io:6379
```

Throttle counters are small, short-lived and reconstructible — losing the whole
cache degrades rate limiting for a minute and breaks nothing else. That is
exactly the workload a free tier is fine for.

### Option B — Memorystore for Redis

Private-IP only, so Cloud Run needs Direct VPC egress:

```bash
gcloud redis instances create edawr-cache \
  --size=1 --region=$REGION --redis-version=redis_7_0

# then add to the deploy in Step 7:
#   --network=default --subnet=default --vpc-egress=private-ranges-only
```

Correct, more private, and roughly the cost of the rest of this deployment put
together. Choose it when something other than throttle counters justifies it.

---

## 4. Secrets

Four values must not be baked into the image or pasted into a deploy command
that ends up in your shell history.

```bash
# Generate the two signing keys — separate values, deliberately. One secret
# signing two things means a leak in either is a leak in both.
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"   # DJANGO_SECRET_KEY
```

```bash
for name in jwt-secret django-secret-key database-url cache-url; do
  gcloud secrets create $name --replication-policy=automatic
done

# Then set each value. --data-file=- reads stdin, so the value never becomes
# an argv entry visible in `ps` or your history.
printf '%s' 'PASTE_JWT_SECRET_HERE'       | gcloud secrets versions add jwt-secret --data-file=-
printf '%s' 'PASTE_DJANGO_SECRET_HERE'    | gcloud secrets versions add django-secret-key --data-file=-
printf '%s' 'postgresql://…neon.tech/…'   | gcloud secrets versions add database-url --data-file=-
printf '%s' 'rediss://…'                  | gcloud secrets versions add cache-url --data-file=-
```

Note `printf` rather than `echo`: `echo` appends a newline, and a trailing
newline inside a database URL produces a connection error that reads as a DNS
failure.

---

## 5. Buckets

Two buckets, and they are separate **on purpose**. Production runs
`SERVE_MEDIA=true`, which makes Django serve everything under `MEDIA_ROOT` to
anyone who asks, with no authentication. A database dump written there would
be a public download of every customer's name, phone number and address,
reachable by guessing a filename. Mounting them as one bucket, or pointing
`BACKUP_DIR` inside `UPLOAD_DIR`, is the whole failure. `manage.py
backup_database` refuses to run in that configuration rather than trusting
anyone to have read this paragraph.

```bash
gcloud storage buckets create gs://${PROJECT_ID}-uploads --location=$REGION
gcloud storage buckets create gs://${PROJECT_ID}-backups --location=$REGION
```

**Neither bucket is made public, including the uploads one.** That is worth
saying because it looks like an omission. Product images are served by
*Django*, off the mounted volume, at `/uploads/<name>` — the upload view
returns a relative path and each client prefixes it with the API origin. The
browser never talks to Cloud Storage, so `allUsers` on the bucket would grant
the internet a second, unthrottled way in and buy nothing.

It becomes the right call only if you later put a CDN in front of the images
and serve them from the bucket directly — at which point turn `SERVE_MEDIA`
back off, because Django would no longer be in that path.

### Service account

```bash
gcloud iam service-accounts create edawr-api --display-name="eDawr API"
export SA=edawr-api@${PROJECT_ID}.iam.gserviceaccount.com

# Read the four secrets
for name in jwt-secret django-secret-key database-url cache-url; do
  gcloud secrets add-iam-policy-binding $name \
    --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
done

# Write to both buckets — objectUser, not objectViewer: the upload view writes.
for b in uploads backups; do
  gcloud storage buckets add-iam-policy-binding gs://${PROJECT_ID}-${b} \
    --member=serviceAccount:$SA --role=roles/storage.objectUser
done
```

---

## 6. Build the image

```bash
gcloud artifacts repositories create $REPO \
  --repository-format=docker --location=$REGION

export IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}
export TAG=$(git rev-parse --short HEAD)

gcloud builds submit --tag ${IMAGE}:${TAG} .
```

Run this from `backend/`. `.dockerignore` keeps `.env`, `.venv/`, the local
SQLite file, `uploads/` and the tests out of the build context — the first two
are correctness rather than tidiness: `.env` would bake your development
secrets into a shipped layer, and `.venv/` holds Windows binaries that would
shadow the Linux virtualenv built in the image.

**Tag with the commit, not `latest`.** The migration job in Step 8 has to run
the *same* image as the service, and "the same" is not a thing `latest` can
promise you.

---

## 7. Deploy

```bash
gcloud run deploy $SERVICE \
  --image=${IMAGE}:${TAG} \
  --region=$REGION \
  --service-account=$SA \
  --allow-unauthenticated \
  --min-instances=1 \
  --max-instances=4 \
  --cpu=1 --memory=512Mi \
  --timeout=60 \
  --set-secrets="JWT_SECRET=jwt-secret:latest,DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest,CACHE_URL=cache-url:latest" \
  --set-env-vars="^|^ENVIRONMENT=production|ALLOWED_HOSTS=PLACEHOLDER|CORS_ORIGINS=https://edawr.example,https://admin.edawr.example|SERVE_MEDIA=true|SERVE_API_DOCS=false|STORE_TIMEZONE=Asia/Kolkata|LOG_LEVEL=INFO" \
  --add-volume mount-path=/app/uploads,type=cloud-storage,bucket=${PROJECT_ID}-uploads,mount-options="uid=10001;gid=10001" \
  --add-volume mount-path=/app/backups,type=cloud-storage,bucket=${PROJECT_ID}-backups,mount-options="uid=10001;gid=10001"
```

`ALLOWED_HOSTS=PLACEHOLDER` is deliberate: you do not know the service
hostname until the service exists. The first deploy will boot and reject every
request with a 400; fix it immediately after:

```bash
export API_HOST=$(gcloud run services describe $SERVICE --region=$REGION \
  --format='value(status.url)' | sed 's#https://##')

gcloud run services update $SERVICE --region=$REGION \
  --update-env-vars="ALLOWED_HOSTS=${API_HOST}"
```

### Why each flag is what it is

- **`^|^` before the env vars** changes the delimiter from `,` to `|`, because
  `CORS_ORIGINS` is itself a comma-separated list. Without it gcloud splits the
  origins into separate variables and the console renders empty.
- **`uid=10001;gid=10001`** matches the `USER 10001:10001` in the Dockerfile.
  A Cloud Storage volume is owned by root by default, so omit this and the
  upload view gets a permission error writing to a mount it owns nothing in.
  The separator inside `mount-options` is a **semicolon** — commas separate the
  keys of `--add-volume` itself — and the value is quoted so the shell does not
  read the semicolon as a command separator.
- **The volume flags differ between services and jobs.** A single-container
  *service* puts the mount path inside `--add-volume` as `mount-path=`, as
  above. A *job* names the volume and mounts it in two flags
  (`--add-volume name=… --add-volume-mount volume=…,mount-path=…`), which is
  the form Step 11 uses. Swapping the two forms is a flag-parsing error, so you
  find out immediately rather than at runtime.
- **Volume mounts require the gen2 execution environment.** Cloud Run selects
  it automatically; do not pin `--execution-environment=gen1`.
- **`SERVE_MEDIA=true`** is a knowing exception. Django's static file server is
  single-threaded and does no caching, but with a GCS volume behind it this is
  the simplest thing that serves product images. Put a CDN in front when image
  traffic justifies it, and turn this back off.
- **`SERVE_API_DOCS=false`** — `/docs` is a complete, interactive map of the
  API. Useful in development, an unnecessary gift to a stranger in production.
  It already defaults to off outside development; setting it explicitly means
  nobody has to reason about the default.
- **`--min-instances=1`** buys you out of cold starts. A cold start here is a
  container pull plus Django's import plus a first connection to Neon — several
  seconds, paid by a customer standing in a checkout flow. This is the single
  most worthwhile line item in the bill.
- **`--timeout=60`** is a request deadline. `config/gunicorn.py` deliberately
  sets gunicorn's own `timeout = 0`, because two timeouts means the shorter one
  wins silently and Cloud Run's is the one that should own this.
- **`--max-instances=4`** bounds your database connections. See Step 9.
- **`--allow-unauthenticated`** is correct: this is a public API whose own
  auth is bearer tokens. IAM here would block the storefront.

### If you turn on rider push notifications

`PUSH_ENABLED` defaults to off, and leaving it off is fine until the rider app
ships with an EAS project id. When you do turn it on, know this:

`api/push.py` defers each send to `transaction.on_commit` and then to a daemon
thread, so the HTTP call to Expo can outlive the response. **Cloud Run throttles
CPU to near zero between requests**, so that thread may not get scheduled until
the next request arrives. Either accept it — notifications are explicitly
best-effort and the rider app's 15-second poll is the source of truth, so the
worst case is one poll interval — or deploy with `--no-cpu-throttling`, which
costs meaningfully more. For a store this size, accept it.

---

## 8. Migrations

Cloud Run does not run migrations for you, and you should not put them in the
container's start-up path: with more than one instance they would race, and a
failed migration would become a crash loop instead of a message.

Run them as a job on the **same image tag**:

```bash
gcloud run jobs deploy ${SERVICE}-migrate \
  --image=${IMAGE}:${TAG} \
  --region=$REGION \
  --service-account=$SA \
  --command=python \
  --args=manage.py,migrate \
  --set-secrets="JWT_SECRET=jwt-secret:latest,DJANGO_SECRET_KEY=django-secret-key:latest,DATABASE_URL=database-url:latest,CACHE_URL=cache-url:latest" \
  --set-env-vars="^|^ENVIRONMENT=production|ALLOWED_HOSTS=${API_HOST}|CORS_ORIGINS=https://edawr.example"

gcloud run jobs execute ${SERVICE}-migrate --region=$REGION --wait
```

The job carries the same secrets and env because `check_production_safety()`
runs on any entry point, `migrate` included. That is a feature: a
misconfiguration is caught before it touches the schema.

**The order for every subsequent deploy is: build → update the job's image →
execute the job → deploy the service.** Migrations that only add things are
safe to run before the new code; anything that drops or renames a column needs
the two-step expand/contract dance, and `api/migrations/0003_quick_commerce` is
the worked example of a migration written to survive existing data.

### The first admin account

Define the job with a harmless placeholder command, then supply the real
arguments **at execution time** — an execution override is not written back
into the job's stored configuration, so the password does not sit in your
project waiting to be read:

```bash
gcloud run jobs deploy ${SERVICE}-admin \
  --image=${IMAGE}:${TAG} --region=$REGION --service-account=$SA \
  --command=python --args=manage.py,check \
  --set-secrets="THE SAME FOUR AS THE MIGRATE JOB" \
  --set-env-vars="THE SAME AS THE MIGRATE JOB"

read -rsp 'New admin password: ' ADMIN_PW; echo
gcloud run jobs execute ${SERVICE}-admin --region=$REGION --wait \
  --args="^|^manage.py|seed_admin|--email|you@example.com|--password|${ADMIN_PW}|--role|admin"
```

`read -rs` keeps the password off the screen and out of your shell history.
The `^|^` delimiter is needed again because `--args` is otherwise
comma-separated, and the account's own arguments contain none.

`seed_admin` creates *or updates* one account and deletes nothing, so it is
safe to re-run — it is also how you recover a locked-out admin later. Sign
in to the console afterwards and change the password anyway.

**Do not run `manage.py seed` against production.** It deletes every row before
inserting sample data, hand-added admins included. `seed_admin` and
`demo_clear` exist precisely because `seed` cannot be run on a live database.

---

## 9. Verify

```bash
curl -s https://${API_HOST}/api/health          # {"status":"ok"}
curl -s https://${API_HOST}/api/health/ready    # checks database and cache
curl -s https://${API_HOST}/api/store/config    # real data from Postgres
```

`/api/health` is liveness and deliberately touches nothing external — if it
checked the database, one brief Neon blip would fail every instance's probe at
once and turn a recoverable dependency failure into a total outage.
`/api/health/ready` is the one that checks, and reports per-dependency so
whoever is paged can see which failed without shelling in.

A cache failure reports `degraded` but does **not** fail readiness: pulling
every instance out of the load balancer because Redis restarted is a bigger
outage than the degraded rate limiting it would prevent.

Then confirm the security posture the deploy is actually running:

```bash
uv run manage.py check --deploy   # with production env vars set
```

CI runs this on every push under production environment variables and it comes
back clean. `security.W003` is silenced deliberately — this API installs no
CSRF middleware because it authenticates with bearer tokens and sets no
cookies, so there is nothing for a forged cross-site request to ride on.

### Connection arithmetic

```
workers × threads × max-instances = peak database connections
   1    ×    8    ×       4       = 32
```

with each held `DB_CONN_MAX_AGE=600` seconds past its last use. Raising
`GUNICORN_THREADS` or `--max-instances` is a database decision at least as much
as a throughput one. Neon's pooler absorbs this comfortably at these numbers;
check the limit on your plan before multiplying either.

---

## 10. Wire up the clients

The API and the two web apps have to agree in both directions:

| Set on | Variable | Value |
|---|---|---|
| Cloud Run | `CORS_ORIGINS` | `https://storefront.example,https://admin.example` |
| Cloud Run | `ALLOWED_HOSTS` | the API's own hostname(s) |
| Storefront | `NEXT_PUBLIC_API_URL` | `https://${API_HOST}` |
| Console | `NEXT_PUBLIC_API_URL` | `https://${API_HOST}` |
| Rider app | its API base URL | `https://${API_HOST}` |

Get `NEXT_PUBLIC_API_URL` wrong and both Next.js apps render empty with no
obvious error: `src/proxy.ts` derives the CSP's `connect-src` and `img-src`
from it, so the browser blocks the catalogue and every product image. It is the
first thing to check when nothing loads.

Get `CORS_ORIGINS` wrong and you get a CORS error in the browser console
instead — noisier, and easier to find.

### Custom domain

```bash
gcloud run domain-mappings create --service=$SERVICE --domain=api.edawr.example --region=$REGION
```

Then **add the new hostname to `ALLOWED_HOSTS`**, keeping the `run.app` one if
anything still calls it. A missing entry is a 400 on every request.

If you later put a CDN or an external load balancer in front, raise
`NUM_PROXIES` to match the number of hops. It defaults to 1, which is exactly
right for Cloud Run alone. Too high and DRF reads a client-forged
`X-Forwarded-For` and every rate limit becomes decoration; too low and everyone
behind one carrier NAT in Aizawl shares a single bucket.

---

## 11. Operations

### Logs

`config/logformat.py` emits one JSON object per line in production, and
`config/gunicorn.py` puts gunicorn's own logs through the same formatter — so
Cloud Logging indexes all of it rather than half of it.

```bash
gcloud run services logs tail $SERVICE --region=$REGION
```

Gunicorn's access log is off by default because Cloud Run's front end already
records every request with status, latency and user agent. Turning it on
(`GUNICORN_ACCESS_LOG=true`) doubles the log bill to say the same thing twice.

### Backups

`manage.py backup_database` shells out to `pg_dump` (installed in the image,
version 17, kept in step with Neon) and rotates to `BACKUP_KEEP` archives.

```bash
gcloud run jobs deploy ${SERVICE}-backup \
  --image=${IMAGE}:${TAG} --region=$REGION --service-account=$SA \
  --command=python --args=manage.py,backup_database \
  --set-secrets="THE SAME FOUR AS THE MIGRATE JOB" \
  --set-env-vars="^|^ENVIRONMENT=production|ALLOWED_HOSTS=${API_HOST}|CORS_ORIGINS=https://edawr.example|BACKUP_DIR=/app/backups" \
  --add-volume name=backups,type=cloud-storage,bucket=${PROJECT_ID}-backups,mount-options="uid=10001;gid=10001" \
  --add-volume-mount volume=backups,mount-path=/app/backups

# Nightly at 02:00 IST
gcloud scheduler jobs create http ${SERVICE}-backup-nightly \
  --location=$REGION \
  --schedule="0 2 * * *" --time-zone="Asia/Kolkata" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${SERVICE}-backup:run" \
  --http-method=POST --oauth-service-account-email=$SA
```

The scheduler authenticates as the same service account, so it needs
permission to start the job — without this the schedule fires nightly and
fails nightly, which is the worst of both:

```bash
gcloud run jobs add-iam-policy-binding ${SERVICE}-backup --region=$REGION \
  --member=serviceAccount:$SA --role=roles/run.invoker
```

Neon also takes its own point-in-time backups. This job is the copy that
survives losing access to the Neon account, which is a different failure.

### Rolling back

Cloud Run keeps every revision:

```bash
gcloud run revisions list --service=$SERVICE --region=$REGION
gcloud run services update-traffic $SERVICE --region=$REGION --to-revisions=REVISION=100
```

A rollback moves the *code* back. It does not move the *schema* back — which is
the reason migrations are written to survive existing data rather than to be
reversed under pressure.

### Pausing the store

Do not redeploy for this. Opening hours, the accept-orders kill switch, the
delivery radius and the store's coordinates live in the `store_settings` table,
editable at `/settings` in the console or by `PATCH /api/settings`. They change
within a shift and the person changing them is behind the counter; needing a
deploy to pause checkout during a power cut means the shop keeps promising
15-minute delivery it cannot make.

Prices, fees and the delivery tiers are the other side of that line — they are
environment variables, and changing one is a deploy, deliberately.

---

## 12. Troubleshooting

| Symptom | Cause |
|---|---|
| Container exits at start with a list of problems | `check_production_safety()` did its job. The log names each one. |
| Every request 400s | `ALLOWED_HOSTS` does not contain the hostname being used. |
| Storefront renders empty, no CORS error | `NEXT_PUBLIC_API_URL` is wrong — the CSP is blocking the API and the images. |
| Storefront renders empty, CORS error | `CORS_ORIGINS` does not list the storefront's origin. |
| Console paginator always claims one page | `X-Total-Count` not readable. `CORS_EXPOSE_HEADERS` is already set — check nothing strips it in front. |
| Image upload returns a permission error | `mount-options=uid=10001;gid=10001` missing from the uploads volume. |
| Uploaded images 404 | `SERVE_MEDIA` is not `true`. |
| Rate limits behave as if absent | `NUM_PROXIES` too high, so DRF keys on a client-forged `X-Forwarded-For` and every caller gets a fresh bucket. (`CACHE_URL` unset cannot be the cause in production — the app would not have booted.) |
| Requests cut off mid-response on deploy | `GUNICORN_GRACEFUL_TIMEOUT` raised above Cloud Run's 10-second grace period. |
| Intermittent 502 with nothing in the app log | Keep-alive mismatch. Raise `GUNICORN_KEEPALIVE` above the idle timeout of whatever is in front. |
| `pg_dump: server version mismatch` | Neon upgraded. Bump `postgresql-client-17` in the Dockerfile to match. |

---

## 13. Known gaps

Read this before proposing something as missing — these are decisions, not
oversights, except where noted.

- **Cash on delivery only.** No payment gateway, and therefore no refunds.
  `payment_method = "cod"` is an intention; `paid_at`, `amount_collected` and
  `collected_by` are what happened.
- **No background worker.** No scheduled dispatch, no delivery-time analytics
  job, no email. Dispatch is a pull feed precisely so that it needs neither a
  scheduler nor a worker to be correct — an offer nobody answers has to expire,
  and something has to expire it. The pull design fails honestly instead: the
  worst case is an order nobody takes, which `GET /api/orders?stalled=true`
  shows the manager directly.
- **No SMS, so `phone_verified_at` is never written.** Setting a password proves
  someone knows a number, not that they hold the SIM, so an unverified customer
  account sees only the orders explicitly linked to it. Closing this needs an
  SMS provider and DLT registration. When it lands, keep the OTP challenge
  **stateless** (a `TimestampSigner` token or a cache key) or the "no migration
  needed" promise on that model field stops being true.
- **No token revocation list.** A leaked token is valid until it expires.
  Deactivating the account, or bumping `token_version`, is the revocation path
  and takes effect on the next request. Per-device revocation would need a
  blacklist outliving the token; this is deliberately coarser.
- **`SERVE_MEDIA=true` is a knowing compromise**, as described in Step 7.
- **Restore is untested.** The backup job runs; nobody has restored from one
  into a scratch database and checked that the store comes up. This is the
  highest-value missing piece in this document — a backup you have never
  restored is a hypothesis, not a backup.
- **The storefront and the rider app have no CI and no version control.** Only
  `backend/` and `admin/` are git repositories. Run their checks by hand before
  shipping either.
