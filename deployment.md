# Deploying the eDawr API to Render

The API only — the storefront, console and rider app deploy separately.

**`render.yaml` is the deployment.** This file explains it. If the two disagree,
the Blueprint is what actually runs.

**On the `runserver` warning:** it is correct and there is nothing to fix.
Production serves with gunicorn via `config/gunicorn.py`. Keep using `runserver`
locally. (gunicorn is POSIX-only; on Windows use
`uv run waitress-serve --listen=127.0.0.1:8000 config.wsgi:application`.)

## Before you start

You need a Neon Postgres URL. Redis you do **not** need to arrange — the
Blueprint creates it.

Both are mandatory. `check_production_safety()` in `api/apps.py` refuses to boot
without them, and the service will exit rather than serve something unsafe.

**Use Neon's pooled endpoint** — the host with `-pooler` in it. This app holds
connections open (`DB_CONN_MAX_AGE=600`) across `WEB_CONCURRENCY ×
GUNICORN_THREADS` threads, and Neon's limit is on *direct* connections; the
pooler exists so a long-lived server can hold handles without spending them.

The direct endpoint has exactly one use: `manage.py test`, which creates and
drops a whole database. The pooler handles that badly — it caches a failed login
for a retry window after the drop, and an interrupted run leaves a half-migrated
database that the next run adopts rather than rebuilds. The symptoms are
alarming and unrelated to your code.

**Neon suspends an idle database** on the smaller plans, so the first query after
a quiet spell pays a wake-up of a few hundred milliseconds. Harmless during a
shift; noticeable on the first order of the morning.

## 1. Deploy

Render Dashboard → **New** → **Blueprint** → connect `edawr-backend` → it reads
`render.yaml` and shows you what it will create:

| | What it is |
|---|---|
| `edawr-api` | the web service, Singapore, Starter, 1 GB disk at `/var/data` |
| `edawr-cache` | Redis for throttle counters, private to your account |
| `edawr-prune-locations` | nightly cron, 03:00 IST |

It will prompt for the eight values marked `sync: false`. Generate the two
secrets first — **two different values**, because one secret signing two things
means a leak in either is a leak in both:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

| Prompt | Value |
|---|---|
| `JWT_SECRET` | a fresh random value |
| `DJANGO_SECRET_KEY` | a *second*, different random value |
| `DATABASE_URL` | your Neon `postgresql://…` URL |
| `ALLOWED_HOSTS` | `edawr-api.onrender.com` — the hostname you expect |
| `CORS_ORIGINS` | the storefront and console origins, comma separated |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | the bucket's API token, scoped to that bucket |
| `R2_PUBLIC_BASE_URL` | the bucket's public origin — the clients need the same value as `NEXT_PUBLIC_MEDIA_URL` |

Render prompts for these **per service**, so it asks twice: once for
`edawr-api`, once for `edawr-prune-locations`. Only the web service's eight are
typed — the cron job's are declared in `render.yaml` as `fromService` references
that copy the web service's values, so the two cannot drift apart.

Note the prompts exist *because* those eight are declared on the services rather
than in the environment group. `sync: false` is ignored inside a group: Render
would silently never set them, and the service would exit at boot listing all
eight as missing. Do not move them back into the group to tidy it up.

The first deploy runs `uv sync --frozen --no-dev`, then `manage.py migrate` as a
**pre-deploy command**, then starts gunicorn. The migration runs against the new
build *before* it takes traffic, and a failure aborts the deploy — which is why
there is no separate migration job to remember.

## 2. Confirm the hostname

`ALLOWED_HOSTS` above is a guess, because the URL only exists once the service
does — Render appends a suffix if `edawr-api` is already taken by someone else.
The guess does not have to be right for the first deploy to succeed: `settings.py`
adds Render's own `RENDER_EXTERNAL_HOSTNAME` to `ALLOWED_HOSTS` at start-up, and
that is the hostname the health check arrives under.

Still, correct it once the service exists, so the value you can read in the
dashboard matches the one in use:

```
edawr-api.onrender.com
```

Add any custom domain here too — `RENDER_EXTERNAL_HOSTNAME` is only ever the
`onrender.com` one, so a custom domain that is not listed 400s every request.

## 3. First admin

Dashboard → `edawr-api` → **Shell**:

```bash
uv run manage.py seed_admin --email you@example.com --password '...' --role admin
```

**Never run `manage.py seed` in production** — it deletes every row.

## 4. Verify

```bash
curl -s https://edawr-api.onrender.com/api/health        # {"status":"ok"}
curl -s https://edawr-api.onrender.com/api/health/ready  # database and cache
curl -s https://edawr-api.onrender.com/api/store/config
```

`/api/health` is also the `healthCheckPath`, so a failing deploy is rolled back
rather than served.

## 5. Wire up the clients

Three settings name a hostname, they answer three different questions, and they
are **not written the same way**. Getting them confused produces two of the
three entries in Troubleshooting below.

| Set on | Variable | Answers | Written as | Example |
|---|---|---|---|---|
| Render | `ALLOWED_HOSTS` | *What `Host` headers do I answer to?* | bare hostnames, comma separated | `api.edawr.in` |
| Render | `CORS_ORIGINS` | *Which browser origins may call me?* | full origins **with scheme** | `https://edawr.in,https://admin.edawr.in` |
| All three clients | `NEXT_PUBLIC_API_URL` | *Where is the API?* | one full URL **with scheme** | `https://api.edawr.in` |

**`ALLOWED_HOSTS` is this API's own hostname, never the frontend's**, and it
carries no scheme, no trailing slash and no path. Django compares it against the
HTTP `Host` header, which never contains `https://` — so a value written as a
URL matches nothing and every request 400s.

**`CORS_ORIGINS` is the frontend's, and it does carry the scheme**, because an
origin is scheme + host + port by definition (`settings.py` feeds it straight to
`CORS_ALLOWED_ORIGINS`). List every spelling a customer might load:
`edawr.in` and `www.edawr.in` are different origins to a browser, and only the
one actually loaded will work.

**`NEXT_PUBLIC_API_URL` is a URL**, and the storefront does more with it than
call it: `src/proxy.ts` builds the CSP's `connect-src` and `img-src` from it.
Wrong here and the browser blocks the catalogue and every product image, with no
CORS error to explain why — the store simply renders empty.

### Custom domains

Dashboard → `edawr-api` → Settings → **Custom Domains**, then create the CNAME
at your registrar and wait for verification.

`settings.py` adds Render's `RENDER_EXTERNAL_HOSTNAME` to `ALLOWED_HOSTS` at
start-up, so the service always answers on its own `onrender.com` name — which
is how the first deploy passes its health check before any domain exists. That
fallback is **only ever the `onrender.com` name**, so a custom domain has to be
listed in `ALLOWED_HOSTS` explicitly or it 400s.

The workable order is: enter the domain you intend to use at the blueprint
prompt, deploy, attach the domain, and change nothing afterwards.

If you put a CDN in front, raise `NUM_PROXIES` to match the hop count — it
defaults to 1, correct for Render alone.

## Configuration in production: there is no `.env`

**`.env` is a development file and it is never deployed.** It is in
`.gitignore`, so it is not in the repository Render clones, and nothing copies it
to the instance. `load_dotenv(BASE_DIR / ".env")` at the top of `settings.py`
simply finds no file there and every value comes from the real environment
instead — which is exactly the intended behaviour, because that `load_dotenv`
call is documented as never overwriting a variable that is already set.

So "update `.env` in production" means **set environment variables on Render**.
There is no file to edit, no file to upload, and creating one on the instance
over SSH would be undone by the next deploy along with the rest of the ephemeral
filesystem.

Do not put production values in your local `.env` either. It is the file your
machine runs against, and a laptop pointed at the production database is one
`manage.py seed` away from deleting every row in it.

### The three places a value can come from

| Where | What lives there | How to change it |
|---|---|---|
| **`render.yaml`**, `envVarGroups` → `edawr-api` | Shared, non-secret values: `ENVIRONMENT`, `STORE_TIMEZONE`, `SERVE_MEDIA`, `SERVE_API_DOCS` | Edit the file, commit, push. Both services pick it up |
| **`render.yaml`**, on a service | `UPLOAD_DIR` (web only), `CACHE_URL` (from the Key Value service), and the eight `sync: false` prompts | Values for the prompts are entered in the Dashboard; the declarations are in the file |
| **Nowhere — unset** | Everything else in `.env.example`. Each has a working default in `settings.py`, and the default is the production value | Add it to the group only when you want something other than the default |

That third row is the one to internalise: **an unset variable is not a missing
one.** `.env.example` documents every knob. Production sets fifteen —
thirteen of them below, plus two Render wires up itself. Everything else runs on a
default chosen for production, and copying it into Render unchanged only creates
a second place that has to agree with the first.

### The thirteen you set

Eight are entered by hand, once, when Render creates the Blueprint. They are
declared `sync: false` **on the services** rather than in the environment group,
because `sync: false` is ignored inside a group — Render would quietly never set
them, and the service would exit at boot listing all eight.

| Variable | Value | Notes |
|---|---|---|
| `JWT_SECRET` | `token_urlsafe(48)` | Signs admin, rider and customer tokens |
| `DJANGO_SECRET_KEY` | a **second**, different `token_urlsafe(48)` | One secret signing two things means a leak in either is a leak in both |
| `DATABASE_URL` | Neon, **pooled** endpoint (`-pooler` in the host) | Must be Postgres — `select_for_update()` is a no-op on SQLite |
| `ALLOWED_HOSTS` | `api.edawr.in` | Bare hostnames. See "Wire up the clients" |
| `CORS_ORIGINS` | `https://edawr.in,https://admin.edawr.in` | Full origins, with scheme |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | the bucket's API token | An R2 token scoped to the one bucket |
| `R2_PUBLIC_BASE_URL` | the bucket's public origin | Also what the clients are built with as `NEXT_PUBLIC_MEDIA_URL` |

Five more are literals in the Blueprint, and are already correct — they are
listed here so you know what they are, not as something to set:

| Variable | Value | Why |
|---|---|---|
| `ENVIRONMENT` | `production` | Turns off `DEBUG`, enables HTTPS enforcement and the start-up safety checks |
| `STORE_TIMEZONE` | `Asia/Kolkata` | What the analytics endpoints bucket by, so "today" is the day the shopkeeper is having |
| `UPLOAD_BACKEND` | `r2` | New images go to the Cloudflare R2 bucket, not the disk |
| `SERVE_MEDIA` | `true` | Django still serves the images written *before* the switch. Becomes `false` when the disk goes |
| `SERVE_API_DOCS` | `false` | `/docs` is a complete map of the API |

And two are wired by Render itself: `CACHE_URL` from the `edawr-cache` service,
and `UPLOAD_DIR` = `/var/data/uploads` on the web service only — the cron job and
the pre-deploy instance mount no disk, so they are deliberately left on the
relative default.

### Defaults you may actually want to change

Everything below is unset in production today. Each is a decision, not an
oversight — check that the default is the decision you want.

| Variable | Default in use | Change it when |
|---|---|---|
| `FREE_DELIVERY_ABOVE` | `199.00` | Your free-delivery threshold is not ₹199 |
| `HANDLING_FEE` | `5.00` | — |
| `MIN_ORDER_VALUE` | `49.00` | — |
| `DELIVERY_FEE_INSTANT` / `_SLOW` | `15.00` / `5.00` | Your two tiers are priced differently |
| `DELIVERY_PROMISE_MINUTES_INSTANT` / `_SLOW` | `15` / `45` | The window you can actually keep is different |
| `PUSH_ENABLED` | `false` | The rider app ships with an EAS project id. Until then this only buys an outbound call per order |
| `AUTO_ASSIGN_RIDER` | `true` | You want the pull feed instead of automatic assignment |
| `LOCATION_PING_RETENTION_DAYS` | `30` | You need a longer or shorter breadcrumb trail |
| `LOGIN_RATE_LIMIT` | `10/min` | Rarely. A four-digit rider PIN is a credential only because of this |
| `SESSION_MAX_HOURS` | `168` | A stolen token should stop renewing sooner than a week |

**These are commercial decisions and they live in the environment on purpose**,
so changing a price takes the care of a deploy. Opening hours, the accept-orders
kill switch, the delivery radius and the store's coordinates are the operational
half and are **not** here — they are rows in `store_settings`, edited from the
console. See the note at the end of `.env.example`.

### Changing a value once you are live

Dashboard → `edawr-api` → **Environment**. Editing a variable restarts the
service, which on a disk-mounted service means the same few seconds of downtime a
deploy costs. Editing the `edawr-api` **environment group** restarts both
services that reference it.

Render preserves environment variables that exist on a service but are absent
from `render.yaml`, so a value you add in the Dashboard survives the next
Blueprint sync. That is convenient and it is also how configuration drifts: a
value that matters belongs in `render.yaml` where the next person can read it,
and only a secret belongs solely in the Dashboard.

Two things must never end up in the repository: the value of any of the eight
prompted variables, and a real `.env`. `.gitignore` covers the second one.
`BACKUP_DIR` must also never be `UPLOAD_DIR` or anything beneath it — production
serves everything under `MEDIA_ROOT` publicly, and a database dump there is a
public download of every customer's name, phone number and address. The command
refuses to run rather than trusting this paragraph to be read.

### Keeping your local `.env` sane

Your machine and production are meant to differ. What matters is knowing which
differences are deliberate:

- `ENVIRONMENT=development` locally, and that is what allows the insecure
  defaults. Never set it to `production` in a local `.env` without also setting
  the eight values above — the app will refuse to boot, correctly.
- `DATABASE_URL` locally may be SQLite, or a **Neon branch** of production, which
  is the better habit: real Postgres semantics with no chance of writing to the
  store's data.
- Use the **pooled** endpoint for running the app and the **direct** endpoint
  (drop `-pooler`) for `manage.py test` only. The test runner creates and drops a
  whole database, which the pooler handles badly.
- Latency is not a detail here. Run the suite against a database in another
  continent and it will not finish; a Neon project in the same region as the
  service is what makes both the tests and the store fast.

## Deploying again

Push to `master`. `autoDeploy: true` builds, runs the migration, and swaps the
service over. Nothing else to do.

**There is a gap of a few seconds where the store is down**, and it is not
avoidable here: attaching a disk disables zero-downtime deploys, because Render
cannot run the old and new instances side by side over one `/var/data`. It stops
this one, then starts the replacement. `graceful_timeout` in `config/gunicorn.py`
is what protects a checkout that is mid-transaction when that happens. Deploy
between shifts if you can.

To roll back: Dashboard → **Events** → *Rollback* on an earlier deploy. That
reverts **code only, never the schema** — a migration that dropped a column is
not undone by rolling back the code that stopped using it.

## Backups

**Neon's point-in-time recovery is the backup.** That is the decision; the rest
of this section is what it means.

Neon continuously archives write-ahead log, so you restore by naming a moment
rather than by finding a file — which is the right shape for the failure that
actually happens here: a bad migration, or a `demo_clear` run against the wrong
database. Restore to the minute before it. Check your plan's retention window in
the Neon console and know the number; on the free tier it is short.

**`manage.py backup_database` cannot run on Render.** It shells out to
`pg_dump`, and Render's native runtimes are Debian 12 with no `apt-get` at build
time. Even where a client exists it would be v15 against Neon's v17, which
`pg_dump` refuses. The command is not dead — run it from your own machine
against the production `DATABASE_URL`:

```bash
BACKUP_DIR=~/edawr-backups uv run manage.py backup_database
```

Worth doing before anything you would want to undo: a migration that drops a
column, a bulk price change, a first real deploy. PITR protects you from your
own mistakes; a dump you hold also protects you from losing access to the Neon
account, which is a different failure and not one Neon can insure you against.

If you later want scheduled dumps back in production, the honest options are a
GitHub Action with the client tools installed, or a container — and the
`Dockerfile` in the repository root already installs `postgresql-client-17` for
exactly this reason. It is the one capability the container has that the native
runtime does not. That is not a reason to choose it: the runtime is a stopgap
for a misconfigured service, and the moment the Blueprint is synced the image
stops being built at all.

## Operations

Logs, shell and metrics are all in the dashboard. The shell is a real one:

```bash
uv run manage.py prune_locations --dry-run     # what the cron would delete
uv run manage.py check --deploy
```

To pause the store, **do not redeploy**. Opening hours and the accept-orders
switch live in the `store_settings` table, editable from the console.

**The nightly prune is not optional.** `order_location_pings` is append-only and
grows with every delivery; unpruned it becomes a permanent record of where your
customers live, and nothing else deletes it. The Blueprint schedules it; if you
remove the cron service, you have removed the retention promise with it.

A non-zero "customer positions on ended orders" count in its output is worth
looking at: `advance_status` deletes those the moment an order ends, so a row
reaching the sweep means something moved an order to a terminal status without
going through the state machine.

## Phone verification

**Everything except the wire is built.** `POST /api/customer/phone/challenge`
issues a stateless signed challenge and sends a six-digit code;
`POST /api/customer/phone/verify` checks it and stamps
`Customer.phone_verified_at`. Both are authenticated, both take the number from
the token's row rather than the body, both sit in the `otp` throttle scope, and
the whole flow is covered by `api/tests/test_phone_verification.py`.

What is missing is a provider, and that is not a library choice. To send an SMS
to an Indian number you need:

1. **A provider account** — MSG91, Gupshup, Kaleyra, Twilio and AWS SNS all
   deliver to India; the first three are the ones with Indian DLT support built
   into their dashboards.
2. **DLT registration**, under TRAI's regime: register the business entity, then
   a sender id (a six-character header), then the template. This is paperwork
   with a fee and a lead time of days, not an API call.
3. **The template registered exactly as sent.** `api/sms.py::otp_message` is the
   string, in one place, for that reason — editing it is a re-registration, not
   only a copy change. An operator drops a message whose body does not match a
   registered template, silently, and the customer simply never receives it.
4. **A backend in `api/sms.py`**, added beside `console`, and `SMS_BACKEND` set
   to it on the service.

Until then leave `SMS_BACKEND=disabled`. The endpoints answer **503** with a
message pointing here, which is the honest outcome: `phone_verified_at` stays
null, and an unverified account sees only the orders placed while signed in to
it — exactly the behaviour the app has always had.

**Never set `SMS_BACKEND=console` on the service.** It writes a working code to
the log, where anyone with a dashboard login can read it, and the endpoint
answers 200 while the customer's phone stays silent. `check_production_safety()`
refuses to boot on it.

### What a verified number unlocks

One rule, in `api/views/customer.py::visible_orders`: a verified account
additionally sees orders that merely *carry* its number and belong to no
account. That is somebody's name, delivery address and order history, so the
code is proving possession of the SIM and nothing weaker. The flow is deliberately
stateless — a signed token, no rows — which means **the `otp` rate limit is the
attempt counter**. Loosening `OTP_RATE_LIMIT` is removing the lock on a
six-digit code, not adjusting a convenience.

## Alerting

**The clients already report their own failures, and until you do this nothing
reads them.** A crash in the storefront, the console, the customer app or the
rider app POSTs to `/api/client-errors`, and a browser that blocks something
posts to `/api/csp-report`. Both land here as one JSON line each. That was worth
building on its own — it is the difference between knowing and a phone call from
the shop — but a log nobody is watching still only helps once somebody thinks to
look.

Render alerts on a log query. Create these three under **Logs → Alerts**, on the
`edawr-api` service:

| Match | Threshold | What it means |
|---|---|---|
| `"message":"client error"` | more than **10 in 5 minutes** | An app is broken for everybody, not for one person on one phone. |
| `"message":"csp violation"` | more than **10 in 5 minutes** | Almost always `NEXT_PUBLIC_API_URL` or `NEXT_PUBLIC_MEDIA_URL` wrong on a fresh deploy — the failure where the site paints and loads nothing. |
| `"level":"ERROR"` AND `"logger":"django.request"` | more than **5 in 5 minutes** | Unhandled 500s. `django.request` is pinned to ERROR in `LOGGING`, so handled 4xx are not in here. |

**A threshold, not a single occurrence**, and the reason is in the shape of the
data. One crash line is one customer on one handset with one broken extension;
it is not actionable and paging on it teaches you to ignore the alert. A broken
deploy produces a *burst* — and the burst is the signal. Pick the numbers to
match your traffic; the ones above assume a quiet shop.

Two fields make an alert triageable once it fires, and both are sent by the
clients rather than derived here:

- **`release`** — which build. On the two Expo apps this matters most: a bad web
  deploy is reverted in a minute, but a bad build sits on customers' phones until
  the stores approve the next one, so several versions are live at once. It is
  `version+buildNumber` there, and the seven-character commit SHA on the web
  (both `next.config.ts` files derive it from `VERCEL_GIT_COMMIT_SHA`, so there is
  nothing to set on Vercel).
- **`route`** — which screen. A route *pattern*, never a real path: `/order/<token>`
  carries a tracking token, which is the whole credential for that order, so the
  clients send `/order/[token]` and `clean()` in `api/views/reports.py` redacts
  anything that slips through — including a CSP report's `document-uri`, which the
  browser composes and no client gets a say in.

There is no paging rotation here and there should not be one for a shop this
size. Email to whoever deploys is the right destination.

## Two constraints worth knowing before you scale

**The disk pins you to one instance, and it is nearly ready to go.** Render
cannot run two copies of a service that mounts a disk, so `numInstances` stays 1
while `/var/data` is attached — and attaching it also disables zero-downtime
deploys. New uploads are *meant* to bypass it: `UPLOAD_BACKEND=r2` writes to
Cloudflare R2 and the browser reads from there.

**Confirm that is actually true of the running service before trusting it.** As
of the last check the bucket was empty and every referenced image 404'd — the
value lives in the `edawr-api` environment group, which the hand-created service
never received, so uploads were still going to a filesystem each deploy threw
away. One command answers it, and it walks the rows rather than the bucket:

```bash
uv run manage.py check_uploads              # is each image in the store?
uv run manage.py check_uploads --public     # ...and can a browser read it?
```

What is left is the images written before the switch. Copy them, then remove it:

```bash
# In the WEB SERVICE's shell — it is the only process that mounts the disk.
uv run manage.py migrate_uploads_to_r2 --dry-run
uv run manage.py migrate_uploads_to_r2
```

Then check the storefront and the console still show every product image, and
delete the `disk:` block, `UPLOAD_DIR` and `SERVE_MEDIA` from `render.yaml`.
Nothing in the database changes at any point: `image_url` holds the relative
path `/uploads/<name>`, which is also the R2 object key with a slash in front.

**Do not move to the free instance type.** It spins down after 15 minutes idle
and cold-starts on the next request. On a 15-minute delivery promise that is most
of the promise, and it suspends the thread `api/push.py` sends notifications on.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Deploy exits at start listing problems | `check_production_safety()` did its job; the log names each one |
| `failed to read dockerfile: open Dockerfile: no such file or directory` | The service's runtime is **Docker**. It was created by hand, back when a `Dockerfile` existed, rather than from `render.yaml`. The `Dockerfile` in the repository root is the stopgap that makes it build; syncing the Blueprint is the fix. See below |
| Those same problems appear as `WARNING` and the service **starts anyway** | `ENVIRONMENT` is not `production` on that service, so the app is running with `DEBUG=True` and the placeholder `JWT_SECRET`. Treat it as an incident, not a warning: see below |
| `Control server error: [Errno 13] Permission denied: '/home/…'` | Harmless. Gunicorn's control socket defaults to `$HOME`, which Render's service user cannot write. `config/gunicorn.py` disables it; if you still see this, the deploy predates that change |
| Every request 400s | `ALLOWED_HOSTS` missing the hostname in use — a custom domain is the usual one, since only the `onrender.com` name is added automatically |
| Deploy exits listing all eight secrets as missing | They were moved into the environment group, where `sync: false` is ignored. Put them back on the services |
| Nightly prune reports nothing, ever | The cron's `DATABASE_URL` is not the store's. It is a `fromService` copy in `render.yaml`; check it was not overridden in the dashboard |
| Storefront empty, no CORS error | `NEXT_PUBLIC_API_URL` wrong — the CSP is blocking the API |
| Storefront empty, CORS error | `CORS_ORIGINS` missing the storefront origin |
| Deploy fails during pre-deploy | A migration failed. The old version is still serving; fix and push again |
| Uploaded images 404, uploads succeed | `R2_PUBLIC_BASE_URL` is wrong, or public access is off for the bucket. An R2 bucket is private until an r2.dev subdomain or a custom domain is turned on |
| Images 404 in the browser only, no server log | The clients' `NEXT_PUBLIC_MEDIA_URL` / `EXPO_PUBLIC_MEDIA_URL` is unset or disagrees with `R2_PUBLIC_BASE_URL`, so the CSP `img-src` blocks them. Needs a rebuild, not a restart |
| Images download instead of rendering | The object was stored without a `ContentType`. Re-upload it; `api/storage.py` sets one from the magic-byte sniff |
| Uploads return 502 | The bucket is unreachable or the key pair is wrong. The reason is in the API log |
| *Legacy* images 404 | `SERVE_MEDIA` is not `true`, or `UPLOAD_DIR` is not on the disk |
| Uploads vanish after a deploy | `UPLOAD_DIR` points outside `/var/data` — the rest of the filesystem is ephemeral |
| **Every image works, then 404s the next day** | `UPLOAD_BACKEND` is not `r2` on the running service, so the bytes went to a filesystem the next deploy discarded — and they are gone, not misplaced. Most likely on the hand-created service, which never received the `edawr-api` environment group that carries the value (see "The service is not the one `render.yaml` describes"). `manage.py check_uploads` names every row whose image is missing; the boot check now refuses `local` outside development unless `UPLOAD_DISK_PERSISTENT=true` |
| Rate limits appear absent | `NUM_PROXIES` too high, or `CACHE_URL` unset |
| First request after a quiet spell is slow | The instance type is `free`. See above |
| Push notifications arrive late or never | Same cause |
| `pg_dump: command not found` | Expected on Render — see Backups |
| Tracking page never shows the rider | Order is not `Dispatched`, no rider assigned, or the last fix is older than `LOCATION_STALE_SECONDS` (90s) |

### The service is not the one `render.yaml` describes

Two symptoms share one cause, and it is worth recognising because neither error
message names it:

```
error: failed to solve: failed to read dockerfile: open Dockerfile: no such file or directory
```

and a `check_production_safety()` warning that printed instead of stopping the
boot. The first says the service's runtime is **Docker**. The second says the
`edawr-api` environment group is not attached. A service created from this
Blueprint would have neither problem — `runtime: python` and `fromGroup` are both
declared there — so the service was created by hand from the repository, at a
time when a `Dockerfile` still existed for Render to detect. Deleting the
`Dockerfile` did not change the service's runtime; it only removed the file that
runtime was still looking for.

**A runtime cannot be changed in the dashboard.** Render supports the change by
API or by Blueprint sync only. Since `render.yaml` already says `runtime:
python`, syncing the Blueprint is the fix, and it attaches the environment group
in the same pass:

> Dashboard → **Blueprints** → the Blueprint for this repository → **Sync**.
> If there is no Blueprint yet: **New** → **Blueprint** → connect
> `edawr-backend`.

Blueprint sync matches existing services **by name**. If your service is called
`edawr-api`, the sync adopts and corrects it. **If it is called anything else,
Render creates a second service** and leaves the Docker one running beside it —
so rename the existing service to `edawr-api` first, or plan to delete the old
one and move the custom domain across.

Two things do not survive replacing a service rather than adopting one: the
`onrender.com` hostname, which must then be added to `ALLOWED_HOSTS`, and the
contents of the mounted disk, which is every product image uploaded so far. The
database is external and is not affected either way.

Once synced, confirm the deploy log opens with `uv sync --frozen --no-dev`
rather than a Docker build, and that no `WARNING:` line follows the gunicorn
banner.

#### The stopgap: let the Docker service build

If the sync has to wait, the repository root carries a `Dockerfile` and a
`docker-entrypoint.sh` that give the Docker-runtime service something to build.
Push them and the deploy goes green without touching the dashboard. Two things
the image cannot supply on its own, because they are service settings rather
than repository contents:

- **Migrations.** `render.yaml` runs them as a `preDeployCommand`; a hand-made
  service has none unless somebody set one. Either set one in the dashboard, or
  set `RUN_MIGRATIONS=true` on the service and the entrypoint applies them
  before gunicorn starts. Set exactly one of the two, never both.
- **Everything `check_production_safety()` demands.** `ENVIRONMENT=production`
  plus `CACHE_URL` from a Key Value instance you would have to create by hand,
  `UPLOAD_DIR` pointing inside a disk you would have to attach, and the eight
  secrets. The Blueprint declares all of it; the Dockerfile declares none of it.

The image runs as uid 10001. The entrypoint starts as root purely to take
ownership of the upload directory — a disk is mounted after the image is built,
with an ownership the build did not choose — and then drops privileges with
`gosu` before exec'ing gunicorn. Without that, the service boots and serves
happily and the *first image upload* 500s.

### A safety warning that did not stop the boot

`check_production_safety()` **raises** outside development and only **prints**
inside it. So a live service that logs

```
WARNING: JWT_SECRET is still the placeholder from .env.example …
```

and then goes on to serve traffic is telling you something the message itself
does not say: `ENVIRONMENT` was never set to `production` on that service. The
warning you can read is the smaller half of the problem. The rest of what
`ENVIRONMENT` gates is off too — `DEBUG` is on, so an exception returns Django's
debug page with the settings and the failing SQL in it; the HTTPS, HSTS and
secure-cookie block at the bottom of `config/settings.py` is skipped; and
`NUM_PROXIES` defaults to the development value, so every rate limit is keyed on
the proxy rather than the caller.

It means the environment group is not attached — a service created by hand in
the dashboard rather than from `render.yaml`, or one whose group was detached
later. Fix it in this order:

1. Set `ENVIRONMENT=production` and the eight prompted values on the service, or
   re-apply the Blueprint so the `edawr-api` group is attached again.
2. Generate a **new** `JWT_SECRET` and `DJANGO_SECRET_KEY` — two different
   values. Do not deploy the placeholder-signed tokens forward: anyone who read
   `.env.example` could mint an admin token while the service was up, and the
   only revocation is changing the secret.
3. Restart. The service now either boots clean or exits naming what is still
   missing — and from here on, that check is fatal, which is the point of it.

Every admin, rider and customer is signed out by step 2, because their tokens
were signed with the old secret. That is the intended cost.
