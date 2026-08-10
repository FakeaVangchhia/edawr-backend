# eDawr backend (Django REST Framework)

The API for the storefront, the admin console, and the rider app.

- **Interactive docs:** http://localhost:8000/docs once running. Every endpoint
  below is listed there and can be called from the browser.
- **Coming from the FastAPI version?** [docs/drf.md](docs/drf.md) is a
  concept-by-concept translation guide written against this codebase. Read that
  before writing any DRF code.
- **Dependency management:** [docs/uv.md](docs/uv.md).

---

## Quick start

Dependencies are managed with [uv](https://docs.astral.sh/uv/). If you don't
have it: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows)
or `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux).

```bash
cd backend

uv sync                          # creates .venv and installs from uv.lock
cp .env.example .env             # optional — every value has a working default.
                                 # JWT_SECRET must be replaced before deploying.

uv run manage.py migrate         # create the schema
uv run manage.py seed            # load sample data
uv run manage.py runserver 8000
```

No virtualenv activation — `uv run` executes inside the project environment.
`runserver` restarts whenever you save a file; drop it in production.

Then point the frontend at it — in `frontend/.env`:

```
NEXT_PUBLIC_API_URL=http://localhost:8000
```

and run `npm run dev` in `frontend/`. That single variable is the only wiring;
every fetch in the app goes through `apiUrl()`/`authFetch()` in
`frontend/src/lib/api.ts`.

**Seeded admin login:** `admin@edawr.local` / `admin1234`

**Testing with the mobile app on a real phone:** bind to all interfaces so the
phone can reach your machine over the LAN —

```bash
uv run manage.py runserver 0.0.0.0:8000
```

The Expo app auto-detects your LAN IP and targets port 8000
(`mobile/src/config.ts`). CORS does not apply to React Native, so no extra
origin config is needed — but `ALLOWED_HOSTS` does, and it defaults to `*` in
development precisely so this works.

---

## Project layout

```
backend/
├── manage.py             every command you run (runserver, migrate, seed, shell)
├── config/               the Django *project*
│   ├── settings.py       all configuration: env, database, CORS, DRF, media
│   ├── urls.py           root URL table; mounts api/, /docs, /uploads
│   ├── wsgi.py           production entry point (sync)
│   └── asgi.py           production entry point (async)
├── api/                  the Django *app* — everything else
│   ├── models.py         the tables
│   ├── serializers.py    request validation + response shapes
│   ├── authentication.py reads the bearer token -> request.user
│   ├── permissions.py    IsAdmin, and the AdminAPIView base class
│   ├── security.py       password hashing + JWT sign/verify
│   ├── exceptions.py     forces every error body into {"detail": "..."}
│   ├── apps.py           startup checks (insecure-config guard, mkdir uploads)
│   ├── urls.py           every URL this API answers, in one table
│   ├── migrations/       schema history — committed, replayed by `migrate`
│   ├── management/commands/seed.py    `manage.py seed`
│   └── views/            one module per resource
│       ├── meta.py           /api/health
│       ├── auth.py           /api/auth/*
│       ├── store.py          /api/store/*        (public)
│       ├── products.py       /api/products/*     (admin)
│       ├── categories.py     /api/categories/*   (admin)
│       ├── orders.py         /api/orders/*       (mixed)
│       ├── users.py          /api/users/*        (admin)
│       ├── delivery.py       /api/delivery/*     (public)
│       └── uploads.py        /api/uploads/*      (admin)
├── docs/
│   ├── drf.md            FastAPI -> DRF learning guide
│   └── uv.md             dependency management guide
├── pyproject.toml        project metadata + direct dependencies
├── uv.lock               exact resolved versions (committed, never hand-edited)
└── .python-version       Python version for this project (3.14)
```

`api/views/` is the old `app/routers/`, split the same way and for the same
reason: one module per resource, chosen by access level where that differs
(`store.py` is public, `products.py` is admin, both read the same table).

---

## Endpoints

Auth column: **admin** = requires `Authorization: Bearer <token>`;
**public** = no auth.

| Method | Path | Auth | Purpose |
| ------ | ---- | ---- | ------- |
| GET | `/api/health` | public | liveness check |
| POST | `/api/auth/login` | public | `{email, password}` → `{access_token, username}` |
| GET | `/api/auth/me` | admin | validate a stored token, get a fresh one |
| GET | `/api/store/products` | public | storefront catalog (active products only) |
| GET | `/api/products` | admin | all products |
| POST | `/api/products` | admin | create product |
| PUT | `/api/products/{id}` | admin | replace product |
| DELETE | `/api/products/{id}` | admin | delete product |
| POST | `/api/uploads/products/image` | admin | multipart upload → `{image_url}` |
| GET | `/api/categories` | admin | list categories |
| POST | `/api/categories` | admin | create category |
| PUT | `/api/categories/{id}` | admin | update category |
| DELETE | `/api/categories/{id}` | admin | delete category |
| GET | `/api/orders` | admin | orders newest-first, items nested |
| POST | `/api/orders/{id}/assign` | admin | manager assigns a rider |
| GET | `/api/users` | admin | staff + riders |
| POST | `/api/users` | admin | create staff member |
| POST | `/api/auth/rider/login` | public | `{phone, pin}` → `{access_token, rider}` |
| GET | `/api/auth/rider/me` | rider | revalidate token, get a fresh one |
| GET | `/api/delivery/riders` | admin | rider roster for manager tooling |
| GET | `/api/delivery/{id}/dashboard` | rider | incoming / active / recent buckets (own only) |
| PATCH | `/api/orders/{id}/status` | rider | rider updates status (own orders only) |
| POST | `/api/orders/{id}/accept` | rider | rider claims an order (no body) |
| POST | `/api/orders/{id}/reject` | rider | rider declines an offer (no body) |

Plus `/docs` (Swagger UI), `/api/schema` (OpenAPI YAML) and `/uploads/<file>`.

Two things changed from the FastAPI contract. **A malformed request body returns
400 rather than 422** — both carry `{"detail": "..."}`, which is all the clients
read. And **rider endpoints now require a rider token**: `accept`, `reject` and
`status` no longer accept a `delivery_boy_id` in the body, because the rider is
whoever holds the token. The Expo app was updated to match; nothing else called
them.

---

## How it fits together

A request to `PUT /api/products/7`:

```
config/urls.py        include("api.urls")
api/urls.py           "api/products/<int:product_id>"  ->  ProductDetailView
                      <int:> refuses a non-numeric id before any view runs
authentication.py     reads the bearer token, sets request.user
permissions.py        IsAdmin says yes (AdminAPIView attached it)
views/products.py     .put() runs
serializers.py        ProductSerializer validates the body, .save() writes
                      the row, .data renders the response
exceptions.py         only if something raised — normalises it to {"detail": ...}
```

The full explanation of each layer, and how it maps to the FastAPI code it
replaced, is in **[docs/drf.md](docs/drf.md)**.

---

## Common commands

```bash
uv run manage.py runserver 8000       # dev server
uv run manage.py makemigrations       # after editing api/models.py
uv run manage.py migrate              # apply migrations (keeps existing data)
uv run manage.py showmigrations       # what is applied, what is pending
uv run manage.py seed                 # reset sample rows (destructive to data)
uv run manage.py shell                # REPL with Django configured
uv run manage.py check                # configuration sanity check
uv run manage.py help                 # every available command
```

---

## Auth flow

1. `AdminLogin.tsx` POSTs `{email, password}` to `/api/auth/login`.
2. `LoginView` verifies the hash, signs a JWT with `sub = email`, returns
   `{access_token, token_type, username}`.
3. The frontend stores it in `sessionStorage` under `edawr-admin-session`.
4. `authFetch` attaches `Authorization: Bearer <token>` to every admin request.
5. `AdminJWTAuthentication` decodes it and loads the `AdminUser` into
   `request.user`; `IsAdmin` rejects the request if that is None or inactive.

Login failures return the same 401 whether the email is unknown or the password
is wrong, so the endpoint cannot be used to enumerate admin accounts.

Tokens are unchanged from the FastAPI backend — same algorithm, same secret,
same claims. **Password hashes are not:** Django's PBKDF2 hashers replaced
bcrypt, so hashes written by the old backend no longer verify. Re-seed.

**Add another admin:**

```bash
uv run manage.py shell -c "from api.models import AdminUser; from api.security import hash_password; AdminUser.objects.create(email='you@example.com'.lower(), password_hash=hash_password('your-password'))"
```

The email **must be stored lowercase** — login normalises the submitted address
before looking it up, so a row stored as `You@Example.com` can never be matched.

---

## Database

SQLite by default (`backend/edawr.db`), zero setup. Tables mirror the old
Supabase schema minus the WhatsApp `messages` table and the unused `todos`
table, plus an `admin_users` table for logins. `Meta.db_table` on every model
pins the original table names.

**Schema changes are migrations now.** This is the biggest change from the
FastAPI setup, where `create_all()` only ever created *missing* tables and
dropping the database was the only way to pick up a model change:

```bash
# edit api/models.py, then
uv run manage.py makemigrations
uv run manage.py migrate
```

Migration files in `api/migrations/` are source code — commit them.

**Moving to Postgres** is one line in `.env`:

```
DATABASE_URL=postgres://user:password@localhost:5432/edawr
```

plus `uv add "psycopg[binary]"`. One thing to change in code when you do:
money columns in `models.py` are `FloatField` because SQLite has no decimal
type. Switch to `DecimalField(max_digits=10, decimal_places=2)` for exact
currency maths, then `makemigrations`.

SQLite foreign keys no longer need hand-holding: Django issues
`PRAGMA foreign_keys=ON` on every SQLite connection itself, so the `connect`
event listener the SQLAlchemy setup needed is gone.

---

## Deploying

Set `ENVIRONMENT` to anything other than `development` and the app refuses to
start while `JWT_SECRET` is still the placeholder from `.env.example`, or while
`ALLOWED_HOSTS` is `*`. That placeholder is committed to this repository, so a
deployment using it would let anyone who knows an admin email forge a valid
admin token.

```bash
ENVIRONMENT=production
JWT_SECRET=$(uv run python -c "import secrets; print(secrets.token_urlsafe(48))")
ALLOWED_HOSTS=api.your-domain
CORS_ORIGINS=https://your-frontend-domain
```

Install with `uv sync --frozen --no-dev` — `--frozen` fails the deploy if
`uv.lock` is stale rather than silently resolving something else.

`runserver` is a development server and must not be used in production. Serve
`config.wsgi:application` with gunicorn (Linux) or waitress (Windows), run
`manage.py migrate` as a release step, and put nginx or object storage in front
of `/uploads/` instead of Django's file server.

---

## Known gaps

- **Nothing creates orders.** Order creation lived inside the deleted WhatsApp
  webhook, so no endpoint replaces it. `manage.py seed` inserts three sample
  orders so the dashboards have data. The natural next step is
  `POST /api/orders` plus a checkout button on the storefront, which currently
  has a cart with nowhere to send it. **This is the main decision waiting for
  you.** Wrap the implementation in `@transaction.atomic` — it has to insert
  items and decrement stock together or not at all.
- **The rider "Reject" button does nothing.** `POST /api/orders/{id}/reject`
  clears `offered_to_delivery_boy_id`, but nothing in the system ever *sets*
  that column — there is no offer/dispatch step, so orders go straight into
  every nearby rider's `incoming` feed. The endpoint returns `{"success": true}`
  and the order reappears on the next refresh. A faithful port of the FastAPI
  route, which was a faithful port of the Supabase one. Making it work needs a
  decision: either a dispatch step that offers an order to one rider at a time,
  or an `order_rejections` table so a decline is remembered per rider.
  **Not implemented — it needs your call on which.**
- **No tests.** The endpoints were verified manually. DRF ships `APITestCase`
  and `APIClient`; `uv add --dev pytest pytest-django` if you prefer pytest.
- **Login throttling counts per process.** Both login endpoints are rate
  limited (`LOGIN_RATE_LIMIT`, default `10/min`, keyed by IP), which is what
  keeps a four-digit rider PIN from being brute-forced — 10,000 possibilities
  is minutes of unthrottled guessing. But DRF stores throttle counters in
  Django's cache, and the default `LocMemCache` is per-process: run four
  gunicorn workers and the effective limit becomes 40/min. Set a shared
  `CACHES` backend (Redis or Memcached) when you deploy with more than one
  worker.
- **No way to change a rider's PIN.** `POST /api/users` sets one at creation
  (write-only `pin` field), but there is no rotate/reset endpoint, so a
  forgotten PIN currently needs a shell. `PUT /api/users/{id}` does not exist
  either.
- **`edawr-sqlalchemy-backup.db`** is the pre-migration SQLite file, kept in case
  you want to copy data out of it. Delete it once you are satisfied.
