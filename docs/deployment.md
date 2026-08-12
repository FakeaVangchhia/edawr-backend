# Deploying eDawr

The target stack, and why each piece is where it is:

| Piece | Runs on | Cost |
|---|---|---|
| API (this repo) | Cloud Run, `asia-south1` (Mumbai) | ₹0 inside the free tier |
| Postgres | Neon free tier | ₹0 (~₹450/mo to remove the idle suspend) |
| Redis (throttle counters) | Upstash | ₹0 up to 10k commands/day |
| Product images | Cloud Storage bucket, mounted as a volume | ~₹10/mo |
| Storefront + admin | Vercel Hobby | ₹0 |

Cloud Run's own free tier is [2M requests, 180k vCPU-seconds and 360k
GiB-seconds a month](https://cloud.google.com/run/pricing). One dark store will
not approach it. The cost risk in this stack is not compute — it is the three
pieces of state, which is what the table above is really about.

**Cloud Run is stateless and this app has state.** The container filesystem is
wiped on every deploy and is not shared between instances, so a SQLite file and
a local `uploads/` directory both quietly stop working the moment you deploy a
second revision or scale to a second instance. That is the whole reason the
first four rows exist.

---

## Why images are a mounted bucket and not public object storage

The obvious design — upload to Cloud Storage, hand back a
`https://storage.googleapis.com/...` URL — is the wrong one *for this codebase*,
and it fails in a way that is annoying to diagnose.

`frontend/src/proxy.ts` builds the Content Security Policy's `img-src` from
`NEXT_PUBLIC_API_URL`. A product image served from a different origin is
therefore blocked by the browser, and the store renders with empty tiles and
CSP violations in the console. Making it work means editing `proxy.ts`,
`uploads.py`, the serializers and `assetUrl()` — four places, to change
something that is not the bottleneck.

Mounting the bucket at `/app/uploads` instead means `api/views/uploads.py` keeps
writing to what it thinks is local disk, URLs stay same-origin and relative, and
**no application code changes at all**. The cost is that image bytes are served
by Django rather than by a CDN, which is why `SERVE_MEDIA=true` below is a
deliberate exception to the advice in `config/urls.py` rather than an oversight.

Revisit this if image traffic ever becomes the thing burning the free tier. The
upgrade path is `django-storages` plus a CDN origin listed in the CSP.

---

## One-time setup

### 1. Postgres (Neon)

Create a project in a region close to Mumbai and copy the **pooled** connection
string — the one with `-pooler` in the hostname.

Take the pooled endpoint, not the direct one. Cloud Run opens a new set of
connections per instance, `DB_CONN_MAX_AGE=600` holds each one open for ten
minutes, and instances come and go on their own schedule. Pointing that at a
direct Postgres endpoint is how you exhaust the connection limit under exactly
the traffic spike you wanted to survive. Cap `--max-instances` as well (step 5).

```
DATABASE_URL=postgres://user:password@ep-xxx-pooler.ap-southeast-1.aws.neon.tech/edawr?sslmode=require
```

> Neon's free tier suspends the database after ~5 minutes idle and takes a
> second or so to wake. Stacked on a Cloud Run cold start, the first customer of
> the morning waits noticeably. Neon's paid tier removes the suspend; see
> "Cold starts" below before spending anything.

### 2. Redis (Upstash)

Create a database and copy the **TLS** URL — the scheme is `rediss://`, two s's.

```
CACHE_URL=rediss://default:password@xxx.upstash.io:6379
```

This is not optional. `check_production_safety()` in `api/apps.py` refuses to
boot without it, because DRF keeps throttle counters in the cache and the
default cache is per-process — which would make `LOGIN_RATE_LIMIT` scale with
your instance count, and that limit is the only thing standing between a
four-digit rider PIN and an exhaustive search.

Watch the free tier's 10,000 commands/day. Every throttled request costs about
two commands, and the customer tracking page polls while an order is open. A
busy day will exceed it; Upstash's pay-as-you-go is a few rupees a month, which
is the right thing to switch to rather than raising the limits.

### 3. Cloud Storage bucket

```bash
gcloud storage buckets create gs://edawr-uploads \
  --location=asia-south1 \
  --uniform-bucket-level-access
```

Keep it private. The bucket is reached through the volume mount, not the public
internet — Django is what serves the bytes.

### 4. Secrets

```bash
gcloud services enable run.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com

python -c "import secrets; print(secrets.token_urlsafe(48))" \
  | gcloud secrets create edawr-jwt-secret --data-file=-
python -c "import secrets; print(secrets.token_urlsafe(48))" \
  | gcloud secrets create edawr-django-secret --data-file=-
printf '%s' "$DATABASE_URL" | gcloud secrets create edawr-database-url --data-file=-
printf '%s' "$CACHE_URL"    | gcloud secrets create edawr-cache-url --data-file=-
```

Two different secrets on purpose. One value signing both API tokens and
everything Django signs means rotating it to contain a leaked admin token also
invalidates every CSRF token — and a leak in either place becomes a leak in
both.

Give the runtime service account read access:

```bash
PROJECT=$(gcloud config get-value project)
SA="$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

for s in edawr-jwt-secret edawr-django-secret edawr-database-url edawr-cache-url; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done

gcloud storage buckets add-iam-policy-binding gs://edawr-uploads \
  --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
```

---

## Deploying

### 5. Build and deploy the API

From `backend/`:

```bash
gcloud run deploy edawr-api \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --max-instances 4 \
  --min-instances 0 \
  --add-volume name=uploads,type=cloud-storage,bucket=edawr-uploads,mount-options="uid=10001;gid=10001;file-mode=644;dir-mode=755" \
  --add-volume-mount volume=uploads,mount-path=/app/uploads \
  --set-secrets "JWT_SECRET=edawr-jwt-secret:latest,DJANGO_SECRET_KEY=edawr-django-secret:latest,DATABASE_URL=edawr-database-url:latest,CACHE_URL=edawr-cache-url:latest" \
  --set-env-vars "ENVIRONMENT=production,SERVE_MEDIA=true,ALLOWED_HOSTS=placeholder.invalid,CORS_ORIGINS=https://placeholder.invalid"
```

`--allow-unauthenticated` is correct here: the storefront catalogue, checkout and
order tracking are deliberately public (see the note at the top of
`api/urls.py`). Cloud Run's IAM is not this app's auth boundary — `api/permissions.py`
is.

**`uid=10001;gid=10001` must match the `USER` in the Dockerfile.** The container
runs unprivileged; without these mount options the bucket arrives owned by root
and every product image upload fails with a permission error at the moment an
admin tries to add a product.

The two placeholders are a genuine circular dependency — the API needs to
know the frontend's origin for CORS, and the frontend needs the API's URL — and
step 8 closes the loop. Deploy, then read the assigned URL:

```bash
gcloud run services describe edawr-api --region asia-south1 --format='value(status.url)'
# https://edawr-api-xxxxx-el.a.run.app
```

Set `ALLOWED_HOSTS` to that hostname (no scheme, no trailing slash):

```bash
gcloud run services update edawr-api --region asia-south1 \
  --update-env-vars "ALLOWED_HOSTS=edawr-api-xxxxx-el.a.run.app"
```

> HTTPS needs no configuration. `SECURE_SSL_REDIRECT` and
> `TRUST_PROXY_SSL_HEADER` both default to true outside development, and
> trusting `X-Forwarded-Proto` is safe here specifically because Cloud Run
> terminates TLS itself and there is no way to reach the container around it.
> That assumption is what the setting's warning in `settings.py` is about — on a
> host reachable directly, a client could forge the header.

### 6. Migrations

Cloud Run has no release phase, so migrations are a separate job running the
same image. Never let a web container migrate on startup — with more than one
instance they race each other.

```bash
IMAGE=$(gcloud run services describe edawr-api --region asia-south1 --format='value(spec.template.spec.containers[0].image)')

gcloud run jobs create edawr-migrate \
  --image "$IMAGE" \
  --region asia-south1 \
  --command python --args manage.py,migrate \
  --set-secrets "JWT_SECRET=edawr-jwt-secret:latest,DJANGO_SECRET_KEY=edawr-django-secret:latest,DATABASE_URL=edawr-database-url:latest,CACHE_URL=edawr-cache-url:latest" \
  --set-env-vars "ENVIRONMENT=production,ALLOWED_HOSTS=edawr-api-xxxxx-el.a.run.app,CORS_ORIGINS=https://placeholder.invalid"

gcloud run jobs execute edawr-migrate --region asia-south1 --wait
```

On later deploys: build, run the job, then route traffic. Update the job's
`--image` each time.

### 7. The first admin

**Do not run `manage.py seed` against production.** It deletes every row before
inserting sample data, and it creates an admin whose password is in
`.env.example`. Create the real admin interactively instead:

```bash
gcloud run jobs create edawr-shell --image "$IMAGE" --region asia-south1 \
  --command python --args manage.py,shell  # ...then create the AdminUser row
```

Simpler, if the catalogue is small: run `manage.py` locally against the
production `DATABASE_URL` for this one-off, then never again.

### 8. Frontend (Vercel)

Import `frontend/` as the project root. Set one environment variable:

```
NEXT_PUBLIC_API_URL=https://edawr-api-xxxxx-el.a.run.app
```

This is read at **build** time, not just at runtime — `proxy.ts` bakes it into
the CSP. Changing it means redeploying, not restarting. If the catalogue loads
empty with CSP errors in the console, this variable is wrong; it is the first
thing to check and it is wrong more often than anything else here.

Then close the loop from step 5:

```bash
gcloud run services update edawr-api --region asia-south1 \
  --update-env-vars "CORS_ORIGINS=https://your-app.vercel.app"
```

Comma-separated if you add a custom domain later. No wildcards — the startup
check rejects `*`, and correctly.

### 9. Rider app

Point the Expo app's API base URL at the same Cloud Run URL and rebuild. It is a
native app, so this ships through a store review rather than a deploy.

---

## Cold starts

With `--min-instances 0`, a request arriving after idle waits for a container to
start — a few seconds for Django, plus up to a second more for Neon to wake. For
a store promising delivery in fifteen minutes, that lands on the first customer
of the morning and on nobody else, because the tracking page's polling keeps an
instance warm for as long as any order is open.

`--min-instances 1` removes it, but an always-on instance consumes roughly 2.6M
vCPU-seconds a month against a 180k free allowance — it leaves the free tier
entirely, for around ₹1,000–2,000/mo. Ship with `0`, and only spend if the
morning latency turns out to bother real customers.

---

## Verifying a deploy

```bash
API=https://edawr-api-xxxxx-el.a.run.app
curl -s $API/api/health          # liveness: the process is up
curl -s $API/api/health/ready    # readiness: it can reach Postgres
curl -s $API/api/store/config    # the storefront's first call
```

Then upload a product image from the admin console and reload it — that is the
one path exercising the bucket mount, and it is the one most likely to be
misconfigured.

If the container never becomes healthy, read the logs: `check_production_safety()`
raises a `RuntimeError` naming exactly which setting is wrong, and it refuses to
boot rather than serve with an insecure one.

```bash
gcloud run services logs read edawr-api --region asia-south1 --limit 50
```

---

## Known sharp edges

- **`manage.py seed` is destructive.** It deletes all rows. It is for
  development, and running it against production empties the store.
- **No automated backups.** Neon's free tier keeps a short restore window; that
  is not a backup strategy for order history. Add `pg_dump` on a schedule before
  taking real money.
- **The migration job's image is pinned.** Update `--image` on every deploy or
  you will run an old migration set against a new schema.
- **Upstash's free tier is a real ceiling**, not a soft one. Watch the command
  count for the first week.
- **Images are served by Django.** Fine at this scale, deliberately (see the top
  of this file), but it is the first thing to move if the free tier gets tight.
