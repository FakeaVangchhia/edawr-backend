# eDawr backend (Django REST Framework)

The API for the storefront, the admin console and the rider app.

- **Interactive docs:** http://localhost:8000/docs once running (development
  only — see `SERVE_API_DOCS`).
- **Coming from the FastAPI version?** [docs/drf.md](docs/drf.md) is a
  concept-by-concept translation guide written against this codebase.
- **Dependency management:** [docs/uv.md](docs/uv.md).

---

## Quick start

Dependencies are managed with [uv](https://docs.astral.sh/uv/). If you don't
have it: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows)
or `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux).

```bash
cd backend

uv sync                          # creates .venv and installs from uv.lock
cp .env.example .env             # optional — every value has a working default

uv run manage.py migrate         # create the schema
uv run manage.py seed            # load sample data
uv run manage.py test            # 446 tests, ~10s against Postgres
uv run manage.py runserver 8000
```

**Seeded admin:** `admin@edawr.local` / `admin1234`
**Seeded rider:** `+919000000002` / PIN `4813`

Point the frontend at it with `NEXT_PUBLIC_API_URL=http://localhost:8000` in
`frontend/.env.local`.

**Testing with the phone:** `uv run manage.py runserver 0.0.0.0:8000`. The Expo
app auto-detects your LAN IP. CORS does not apply to React Native, but
`ALLOWED_HOSTS` does — it defaults to `*` in development precisely so this works.

---

## Project layout

```
backend/
├── manage.py             every command you run
├── config/               the Django *project*
│   ├── settings.py       all configuration
│   ├── logformat.py      JSON log formatter for production
│   └── urls.py           root URL table; /docs and /uploads are conditional
├── api/                  the Django *app*
│   ├── models.py         tables + the Order state machine
│   ├── pricing.py        what an order costs. The only place money is computed
│   ├── checkout.py       place / cancel an order, transactionally
│   ├── validators.py     phone normalisation
│   ├── paging.py         clamped limit/offset
│   ├── serializers.py    request validation + response shapes
│   ├── authentication.py bearer token -> request.user
│   ├── permissions.py    IsAdmin / IsRider / IsAdminOrRider
│   ├── security.py       password + PIN hashing, JWT sign/verify
│   ├── exceptions.py     forces every error body into {"detail": "..."}
│   ├── apps.py           startup checks (refuses to boot on insecure config)
│   ├── urls.py           every URL, marked public or guarded
│   ├── migrations/       schema history — committed, replayed by `migrate`
│   ├── tests/            446 tests
│   └── views/            one module per resource
└── pyproject.toml / uv.lock
```

---

## Endpoints

**public** = no token. **admin** / **rider** = that bearer token required.

| Method | Path | Auth | Purpose |
| ------ | ---- | ---- | ------- |
| GET | `/api/health` | public | liveness — touches nothing |
| GET | `/api/health/ready` | public | readiness — checks the database, 503 if down |
| POST | `/api/auth/login` | public | `{email, password}` → `{access_token, username}` |
| GET | `/api/auth/me` | admin | validate a stored token, get a fresh one |
| POST | `/api/auth/rider/login` | public | `{phone, pin}` → `{access_token, rider}` |
| GET | `/api/auth/rider/me` | rider | revalidate, get a fresh token |
| GET | `/api/store/config` | public | promise minutes + fees, so the UI hardcodes nothing |
| GET | `/api/store/products` | public | catalogue; `?q=` `?category=` `?limit=` `?offset=` |
| GET | `/api/store/categories` | public | category rail, with product counts |
| POST | `/api/store/quote` | public | price a basket without placing it |
| POST | `/api/store/orders` | public | **place an order** |
| GET | `/api/store/orders/{token}` | public | track by unguessable token |
| POST | `/api/store/orders/{token}/cancel` | public | customer cancels; restores stock |
| GET | `/api/products` | admin | all products, with cost price |
| POST/PUT/DELETE | `/api/products[/{id}]` | admin | product CRUD |
| POST | `/api/uploads/products/image` | admin | multipart → `{image_url}` |
| GET/POST/PUT/DELETE | `/api/categories[/{id}]` | admin | category CRUD |
| GET/POST | `/api/users` | admin | staff + riders |
| PUT/DELETE | `/api/users/{id}` | admin | update (incl. PIN rotation), deactivate |
| GET | `/api/orders` | admin | `?status=` `?open=true` `?stalled=true` |
| POST | `/api/orders/{id}/assign` | admin | manager assigns a rider |
| PATCH | `/api/orders/{id}/status` | admin+rider | move the order; role decides which moves |
| POST | `/api/orders/{id}/accept` | rider | claim a Ready order |
| POST | `/api/orders/{id}/reject` | rider | decline — remembered per rider |
| GET | `/api/delivery/riders` | admin | rider roster |
| PATCH | `/api/delivery/availability` | rider | the rider's own on/off switch |
| GET | `/api/delivery/{id}/dashboard` | rider | own feed only |

---

## The order lifecycle

```
Placed → Packing → Ready → Dispatched → Delivered
   └────────┴────────┴─────────────────→ Cancelled
                      Dispatched → Ready  (rider hands it back)
```

Declared in `Order.TRANSITIONS`, enforced by `Order.advance_status()`, which
stamps `packed_at` / `dispatched_at` / `delivered_at` / `cancelled_at` exactly
once each. An illegal move raises, and views turn that into a **409** — it is a
conflict with the order's state, not a malformed request.

Who may request what is separate from what is legal: `ADMIN_TARGETS` and
`RIDER_TARGETS` in `views/orders.py`. A rider can never cancel (that decision,
and the refund conversation behind it, belongs to the store); a manager can never
dispatch (that means a specific rider physically took it).

---

## Two design decisions worth knowing

### Dispatch is a pull, and reject is remembered

Every available rider in range sees every `Ready` order **except ones they have
declined**; first to accept wins, the loser gets a 409. Declines live in
`order_rejections`.

The alternative — offering to one rider at a time with a timeout — needs a
scheduler and a background worker to be correct, because an offer nobody answers
has to expire and something has to expire it. The pull design needs neither and
fails honestly: the worst case is an order nobody takes, which
`GET /api/orders?stalled=true` shows the manager directly.

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
`Meta.db_table` so the schema matches the SQLAlchemy and Supabase versions it
came from.

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

The test runner creates and drops `test_edawr`, so the role needs `CREATEDB`:

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

**The full runbook is `deployment.md` in this repository** — architecture,
step by step, every environment variable, and an honest list of what is still
missing. What follows is only the startup contract.

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
```

Install with `uv sync --frozen --no-dev` — `--frozen` fails the deploy if
`uv.lock` is stale rather than silently resolving something else.

Serve with **gunicorn**, configured in `config/gunicorn.py` — which is what the
Dockerfile's `CMD` runs, and where the worker model, the timeouts and the proxy
trust decision each carry the reason they hold that value. `manage.py runserver`
is a development tool and says so on start-up; that warning is about the
existence of `config/gunicorn.py`, not about a problem.

gunicorn is POSIX-only, so on Windows nothing here runs natively. `waitress` is
in the dev dependency group for local checks only (`uv run waitress-serve
--listen=127.0.0.1:8000 --threads=8 config.wsgi:application`); it is never
deployed, reads none of `config/gunicorn.py`, and `uv sync --no-dev` keeps it
out of the image. To exercise the real server configuration, build the
container.

Run `manage.py migrate` as a release step. Put nginx or object storage in front of
`/uploads/` and leave `SERVE_MEDIA` off — Django's static server is
single-threaded, does no caching and supports no range requests.

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
- **`edawr-sqlalchemy-backup.db`** and **`edawr.db`** are pre-Postgres SQLite
  files. Nothing reads either; delete them once you are satisfied.
