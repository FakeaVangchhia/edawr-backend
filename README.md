# eDawr backend (Django REST Framework)

The API for the storefront, the admin console and the rider app.

- **Interactive docs:** http://localhost:8000/docs once running (development
  only — see `SERVE_API_DOCS`).
- **How this project uses DRF, and why:** [docs/drf.md](docs/drf.md).
- **Dependency management:** [docs/uv.md](docs/uv.md).
- **Deploying:** [deployment.md](deployment.md).

---

## Quick start

Dependencies are managed with [uv](https://docs.astral.sh/uv/). If you don't
have it: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows)
or `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux).

```bash
cd edawr-backend

uv sync                          # creates .venv and installs from uv.lock
cp .env.example .env             # optional — every value has a working default

uv run manage.py migrate         # create the schema
uv run manage.py seed            # load sample data
uv run manage.py test            # 640 tests, ~20s against Postgres
uv run manage.py runserver 8000
```

**Seeded admin:** `admin@edawr.local` / `admin1234`
**Seeded rider:** `+919000000002` / PIN `4813`

Point the storefront and the console at it with
`NEXT_PUBLIC_API_URL=http://localhost:8000` in their `.env.local`.

**Testing with the phone:** `uv run manage.py runserver 0.0.0.0:8000`. The Expo
app auto-detects your LAN IP. CORS does not apply to React Native, but
`ALLOWED_HOSTS` does — it defaults to `*` in development precisely so this works.

---

## Project layout

```
edawr-backend/
├── manage.py             every command you run
├── config/               the Django *project*
│   ├── settings.py       all configuration, every knob read from the environment
│   ├── gunicorn.py       the production server, with the reason for each value
│   ├── logformat.py      JSON log formatter for production
│   ├── handlers.py       JSON 404/500 bodies for anything outside DRF
│   └── urls.py           root URL table; /docs and /uploads are conditional
├── api/                  the Django *app*
│   ├── models.py         tables + the Order state machine
│   ├── pricing.py        what an order costs. The only place money is computed
│   ├── checkout.py       place / cancel / restock an order, transactionally
│   ├── dispatch.py       which rider gets a Ready order
│   ├── push.py           Expo push notifications, best-effort
│   ├── location.py       live rider and customer positions (dormant — see Known gaps)
│   ├── storage.py        product images: local disk or Cloudflare R2
│   ├── audit.py          who changed what
│   ├── validators.py     phone normalisation and coordinate checks
│   ├── paging.py         clamped limit/offset, validated filters, date ranges
│   ├── serializers.py    request validation + response shapes
│   ├── authentication.py bearer token -> request.user
│   ├── permissions.py    IsAdmin / IsOwnerAdmin / IsRider / IsCustomer
│   ├── throttling.py     one throttle class per identity
│   ├── security.py       password + PIN hashing, JWT sign/verify
│   ├── exceptions.py     forces every error body into {"detail": "..."}
│   ├── schema.py         OpenAPI security schemes for /docs
│   ├── apps.py           startup checks (refuses to boot on insecure config)
│   ├── urls.py           every URL, marked public or guarded
│   ├── management/       seed, seed_admin, demo_clear, check_uploads,
│   │                     migrate_uploads_to_r2, prune_locations, backup_database
│   ├── migrations/       schema history — committed, replayed by `migrate`
│   ├── tests/            640 tests
│   └── views/            one module per resource
└── pyproject.toml / uv.lock
```

---

## Endpoints

**public** = no token. **staff** = either console role. **Admin** = the Admin
role only. **rider** / **customer** = that bearer token. `api/urls.py` is the
authoritative list, with the reasoning beside each group.

| Method | Path | Auth | Purpose |
| ------ | ---- | ---- | ------- |
| GET | `/api/health` | public | liveness — touches nothing |
| GET | `/api/health/ready` | public | readiness — checks the database, 503 if down |
| POST | `/api/client-errors`, `/api/csp-report` | public | crash and CSP reports from the clients, throttled, fields allowlisted |
| POST | `/api/auth/login` | public | `{email, password}` → `{access_token, email, name, role}` |
| GET | `/api/auth/me` | staff | validate a stored token, get a fresh one |
| POST | `/api/auth/logout` | staff | retire every token this account holds |
| POST | `/api/auth/rider/login` | public | `{phone, pin}` → `{access_token, rider}` |
| GET / POST | `/api/auth/rider/me`, `/api/auth/rider/logout` | rider | revalidate; sign out everywhere |
| POST | `/api/auth/customer/signup`, `/api/auth/customer/login` | public | `{phone, password}` → `{access_token, customer}` |
| GET / PATCH | `/api/auth/customer/me` | customer | revalidate; change the name |
| POST | `/api/auth/customer/password`, `/api/auth/customer/logout` | customer | change password (returns a fresh token); sign out everywhere |
| GET | `/api/customer/orders` | customer | this account's order history |
| POST | `/api/customer/orders/claim` | customer | attach an order by its tracking token |
| POST / DELETE | `/api/customer/push-token` | customer | register / forget a handset |
| GET | `/api/store/config` | public | promise, tiers, fees, opening hours, delivery area |
| GET | `/api/store/products[/{id}]` | public | catalogue; `?q=` `?category=` `?sort=popular` `?limit=` `?offset=` |
| GET | `/api/store/categories` | public | category rail, with product counts |
| GET | `/api/store/promos` | public | the home-page banners that are live now |
| POST | `/api/store/suggestions` | public | an answer to the poll sticker |
| POST | `/api/store/quote` | public | price a basket without placing it |
| POST | `/api/store/orders` | public | **place an order**; `Idempotency-Key` header makes a retry safe |
| GET | `/api/store/orders/{token}` | public | track by unguessable token |
| POST | `/api/store/orders/{token}/cancel` | public | customer cancels; restores stock |
| GET / POST | `/api/store/orders/{token}/rider-location`, `.../location` | public | the rider's position; the customer's own (dormant — see Known gaps) |
| GET / POST | `/api/products` | staff | catalogue with cost price; `?q=` `?category=` `?status=` `?stock=low\|out` |
| GET / PATCH / DELETE | `/api/products/{id}` | staff | one product; PATCH is the only write, on purpose |
| POST | `/api/uploads/products/image` | staff | multipart → `{image_url}` |
| GET / POST / PUT / DELETE | `/api/categories[/{id}]` | staff | category CRUD; a rename carries its products |
| GET / POST / PUT / DELETE | `/api/promos[/{id}]` | staff | banner CRUD |
| GET | `/api/suggestions` | staff | poll answers, newest first |
| GET / POST | `/api/users` | staff | staff + riders; `?role=` `?q=` `?active=` |
| PUT / DELETE | `/api/users/{id}` | staff | update (incl. PIN rotation); deactivate |
| GET / PATCH | `/api/settings` | staff | opening hours, the pause switch, the delivery radius |
| GET | `/api/analytics/{summary,revenue,products,categories,delivery,inventory,cash}` | staff | the console's figures; `?from=` `?to=` |
| GET | `/api/orders` | staff | `?status=` `?open=true` `?stalled=true` `?rider=` `?q=` `?from=` `?to=` |
| POST | `/api/orders/{id}/assign` | staff | manager assigns a rider |
| POST | `/api/orders/{id}/restock` | staff | return a failed delivery's goods to the shelf |
| PATCH | `/api/orders/{id}/status` | staff + rider | move the order; role decides which moves |
| POST | `/api/orders/{id}/accept`, `.../reject` | rider | claim a Ready order; decline — remembered per rider |
| GET | `/api/delivery/riders` | staff | rider roster |
| GET | `/api/delivery/locations` | staff | every rider's last position (dormant) |
| PATCH | `/api/delivery/availability` | rider | the rider's own on/off switch |
| POST / DELETE | `/api/delivery/push-token` | rider | register / forget a handset |
| POST | `/api/delivery/location` | rider | report a position (nothing sends one yet) |
| GET | `/api/delivery/{id}/dashboard` | rider | own feed only |
| GET / POST / PUT / DELETE | `/api/admins[/{id}]` | Admin | console accounts and roles |
| GET | `/api/audit` | Admin | who changed what |

---

## The order lifecycle

```
Placed → Packing → Ready → Dispatched → Delivered
   └────────┴────────┴─────────────────→ Cancelled
                Ready → Packing          (bag reopened)
                      Dispatched → Ready  (rider hands it back)
                      Dispatched → Failed (attempted, did not happen)
```

Declared in `Order.TRANSITIONS`, enforced by `Order.advance_status()`, which
stamps `packed_at` / `dispatched_at` / `delivered_at` / `cancelled_at` exactly
once each — and, on Delivered, the cash record (`paid_at`, `amount_collected`).
An illegal move raises, and views turn that into a **409** — it is a conflict
with the order's state, not a malformed request. `Failed` is terminal and
restores nothing; `POST /api/orders/{id}/restock` is the separate step, once
the goods are back.

Who may request what is separate from what is legal: `ADMIN_TARGETS` and
`RIDER_TARGETS` in `views/orders.py`. A rider can never cancel (that decision,
and the refund conversation behind it, belongs to the store); a manager can never
dispatch (that means a specific rider physically took it).

---

## Two design decisions worth knowing

### Dispatch assigns outright, the feed is the fallback, and reject is remembered

The moment an order is marked Ready, `api/dispatch.py` hands it to the nearest
eligible rider in the same transaction. When nobody qualifies, every available
rider in range sees it in their feed **except ones who have declined it**; first
to accept wins, the loser gets a 409. Declines live in `order_rejections`.

Offering to one rider at a time with a timeout would need a scheduler and a
background worker, because an offer nobody answers has to expire and something
has to expire it. Assigning outright needs neither and fails honestly: the worst
case is an order nobody takes, which `GET /api/orders?stalled=true` shows the
manager directly.

### Money is Decimal, computed only here

`api/pricing.py` is the only module that decides what anything costs. The
checkout request carries product ids and quantities and nothing else — no price,
no fee, no total, and the server reads none from it. A checkout that trusts a
client-supplied total is one where the customer picks the price.

Rounding is ROUND_HALF_UP, not Python's default ROUND_HALF_EVEN, because the
latter rounds 0.125 to 0.12 and makes a bill look wrong for reasons nobody wants
to explain at a doorstep.

---

## Common commands

```bash
uv run manage.py runserver 8000       # dev server
uv run manage.py test                 # the suite
uv run manage.py makemigrations       # after editing api/models.py
uv run manage.py migrate              # apply (keeps existing data)
uv run manage.py seed                 # reset sample rows (destructive to data)
uv run manage.py check --deploy       # Django's deployment checklist
uv run manage.py shell                # REPL with Django configured
```

---

## Database

**PostgreSQL, in development as well as production.** Every model pins
`Meta.db_table`, so a table name is a decision here rather than something Django
derives.

```
DATABASE_URL=postgres://edawr:password@localhost:5432/edawr
```

`psycopg[binary]` is already a dependency, so this is genuinely one line.

SQLite is no longer the default and should not be used again. This is not a
preference: SQLite serialises every write against the whole database and has no
row locks, so the `select_for_update()` in `checkout.py` — the thing that stops
the last unit of stock being sold twice — is a **no-op** there. A test suite that
passes on SQLite leaves the invariant it exists to protect entirely unverified,
which is why local development, CI and production all run Postgres.

The test runner creates and drops `test_<database>`, so the role needs `CREATEDB`:

```
psql -U postgres -c "ALTER ROLE edawr CREATEDB;"
```

**Migrations must survive existing data.** `0003_quick_commerce` is the worked
example: it renames the old status vocabulary, backfills totals from line items,
dedupes category names *before* applying a unique constraint, and populates
tracking tokens row by row *before* making that column unique — a single
`AddField` with a callable default evaluates it once and gives every row the same
value, which for a tracking token would mean any holder could read every order.

---

## Deploying

**The full runbook is `deployment.md` in this repository.** Render, Neon for
Postgres, a Render Key Value instance for throttle counters, and a nightly cron
that prunes location history. `render.yaml` describes the intended service on
Render's native Python runtime; the live service was created by hand with the
Docker runtime and builds from the `Dockerfile` until it is re-created from the
Blueprint — `deployment.md` explains both. What follows is only the startup
contract.

`api/apps.py` refuses to boot outside development while any of these is true.
Each is exploitable, not merely untidy:

| Problem | Why it matters |
| ------- | -------------- |
| `JWT_SECRET` is the placeholder | It is published in this repo; anyone knowing an admin email can forge an admin token |
| `ALLOWED_HOSTS` is `*` or empty | Host-header poisoning |
| `DJANGO_SECRET_KEY` unset | It falls back to JWT_SECRET; one secret signing two things means a leak in either is a leak in both |
| `CACHE_URL` unset | Throttle counters go in per-process memory, so every rate limit is silently multiplied by the worker count — and the login limit is what makes a 4-digit rider PIN a credential |
| `CORS_ORIGINS` empty or `*` | Either the frontend cannot call the API, or anyone can |

```bash
ENVIRONMENT=production
JWT_SECRET=$(uv run python -c "import secrets; print(secrets.token_urlsafe(48))")
DJANGO_SECRET_KEY=$(uv run python -c "import secrets; print(secrets.token_urlsafe(48))")
ALLOWED_HOSTS=api.your-domain
CORS_ORIGINS=https://your-frontend-domain
CACHE_URL=redis://your-redis:6379/0
DATABASE_URL=postgres://user:password@host:5432/edawr
UPLOAD_BACKEND=r2                 # plus R2_ENDPOINT_URL, R2_BUCKET, the key pair
R2_PUBLIC_BASE_URL=https://...    # and what the clients get as NEXT_PUBLIC_MEDIA_URL
```

`UPLOAD_BACKEND=local` is refused outside development unless
`UPLOAD_DISK_PERSISTENT=true` says the directory really survives a deploy.

These are **environment variables set on the platform, not a `.env` file** —
`.env` is gitignored and never deployed. Note that `ALLOWED_HOSTS` takes bare
hostnames (no scheme; it is matched against the `Host` header) while
`CORS_ORIGINS` takes full origins including `https://`. On Render, `CACHE_URL`
is wired from the Key Value service automatically and the rest are prompted for
once. "Configuration in production" in `deployment.md` is the complete account,
including which of the knobs in `.env.example` production sets and which are
deliberately left on their defaults.

Install with `uv sync --frozen --no-dev` — `--frozen` fails the deploy if
`uv.lock` is stale rather than silently resolving something else.

Serve with **gunicorn**, configured in `config/gunicorn.py` — which is what
`render.yaml`'s `startCommand` runs, and where the worker model, the timeouts
and the proxy trust decision each carry the reason they hold that value.
`manage.py runserver` is a development tool and says so on start-up; that
warning is about the existence of `config/gunicorn.py`, not about a problem.

gunicorn is POSIX-only, so on Windows nothing here runs natively. `waitress` is
in the dev dependency group for local checks only (`uv run waitress-serve
--listen=127.0.0.1:8000 --threads=8 config.wsgi:application`); it is never
deployed, reads none of `config/gunicorn.py`, and `uv sync --no-dev` keeps it
out of production.

Run `manage.py migrate` as a release step — `render.yaml` does this with
`preDeployCommand`, so it runs against the new build before it takes traffic.
New images go to R2 and the browser fetches them from there. `SERVE_MEDIA=true`
keeps Django serving the images that were uploaded to the disk before the switch
— its static server is single-threaded and does no caching, so it is a bridge,
not a CDN; it goes with the disk.

Point your orchestrator's **liveness** probe at `/api/health` and its
**readiness** probe at `/api/health/ready`. They are deliberately different:
liveness touches nothing, because a database blip that failed every replica's
liveness check at once would get them all restarted and turn a recoverable
dependency failure into a total outage.

---

## Known gaps

- **Cash on delivery only.** No payment gateway. `payment_method` exists and
  `PAYMENT_CHOICES` has one entry.
- **No refunds.** Cancelling restores stock and marks the order; money is out of
  scope because money never came in.
- **No token revocation list.** A leaked token is valid until it expires (12h).
  Deactivating the user is the revocation path, and it takes effect immediately.
- **Straight-line distance.** Rider radius uses haversine; Aizawl is built on
  ridges, so road distance can be several times it. It decides whether an order
  is plausibly in a rider's area and is never presented as an ETA.
- **No background worker**, so there is no scheduled dispatch, no delivery-time
  analytics job, and no email/SMS.
- **Live location is dormant.** The tables, routes, cron and console map exist
  and are tested; no client sends a position yet. The rider app is where that
  lands.
- **Phone numbers are never verified.** `Customer.phone_verified_at` is read
  everywhere and written nowhere until there is an SMS provider; an unverified
  account sees only the orders placed while signed in to it.
