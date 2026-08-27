# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Do not `git push`. Stage and commit; leave pushing to the user.

## Overview

This repository is `edawr-backend` — **the API**, and the only place business
rules live. eDawr is a quick-commerce grocery platform for Aizawl, Mizoram:
customers order from a web storefront and a rider delivers on a 15-minute
promise.

Django 6 + DRF + **PostgreSQL**. Three clients call it and none of them holds a
rule of its own:

- **`../frontend`** — Next.js customer storefront. UI only, **not version
  controlled**.
- **`../admin`** — Next.js staff console, port 3001. Its own repository
  (`edawr-admin`), its own `CLAUDE.md`, its own deployment.
- **`../mobile`** — Expo rider app. **Not version controlled**, no tests.

The containing directory `F:\Projects\eDawr` **is not a git repository and must
not become one.** Run git only from inside this repository or `../admin`.
`../PRODUCTION.md` is the deployment runbook for all four; Part 4 of it is the
current gap list — read that before proposing something as "missing".

`../admin/CLAUDE.md` restates the API contracts below (money, the state machine,
401-vs-403, the two roles) because that file travels with its own repository.
**If you change one of those contracts, change both**, or the next person in the
console repo reads a rule that is no longer true.

The API was migrated from FastAPI/SQLAlchemy/Pydantic. No FastAPI code remains;
do not reintroduce it. `docs/drf.md` is the concept-by-concept translation guide
if you meet code that reads like it came from there.

## Commands

**Dependencies are managed with uv, not pip** — there is no `requirements.txt`.
Never run `pip install`; use `uv add`, which updates `pyproject.toml` and
`uv.lock` together. See `docs/uv.md`.

```bash
uv sync                                  # install from uv.lock
uv run manage.py migrate                 # create/update the schema
uv run manage.py seed                    # sample data — DELETES ALL ROWS
uv run manage.py runserver 8000          # 0.0.0.0:8000 to reach it from the phone
uv run manage.py makemigrations          # after editing api/models.py
uv run manage.py test                    # 538 tests, ~18s on Postgres
uv run manage.py check --deploy          # before shipping
```

Run one test module, class or method — the suite is fast, but this is faster:

```bash
uv run manage.py test api.tests.test_checkout
uv run manage.py test api.tests.test_checkout.CheckoutTests.test_stock_is_decremented
uv run manage.py test api.tests.test_checkout --keepdb   # skip create/drop
```

Because `seed` is destructive and cannot be re-run against live data, three
other commands exist:

```bash
uv run manage.py seed_admin --email you@example.com --password '...' --role admin
uv run manage.py demo_clear --dry-run     # then without the flag
uv run manage.py backup_database --dry-run
```

Seeded credentials: admin `admin@edawr.local` / `admin1234`, rider
`+919000000002` / PIN `4813`. Interactive docs at `/docs` when `SERVE_API_DOCS`.

**Postgres in development too, not SQLite.** `select_for_update()` — the thing
that stops the last unit of stock being sold twice — is a **no-op** on SQLite, so
a suite that passes there leaves the invariant it exists to protect entirely
unverified. The test runner creates and drops `test_edawr`, so the role needs
`CREATEDB`.

CI (`.github/workflows/ci.yml`) runs against a Postgres service container:
`uv sync --frozen`, `makemigrations --check`, `migrate`, `test`, and
`check --deploy` under production environment variables.

## Where the code lives

`config/` is the Django *project* (settings, WSGI/ASGI, gunicorn, log format,
error handlers). `api/` is the single Django *app*. The modules that carry
design rather than plumbing:

| Module | What it owns |
|---|---|
| `api/models.py` | tables, and `Order.TRANSITIONS` / `advance_status()` |
| `api/pricing.py` | **the only place money is computed** |
| `api/checkout.py` | place / cancel / restock, transactionally |
| `api/dispatch.py` | which riders may see an order, and auto-assign |
| `api/authentication.py` | *who is this?* — never rejects |
| `api/permissions.py` | *may they?* — rejects |
| `api/throttling.py` | one rate-limit class per identity table |
| `api/security.py` | password/PIN hashing, JWT sign + verify |
| `api/audit.py` | one `AuditLog` row per mutating admin action |
| `api/push.py` | best-effort Expo notifications |
| `api/location.py` | live rider/customer positions, and their retention |
| `api/urls.py` | **the complete routing table, marked public or guarded** |
| `api/views/` | one module per resource |

`api/urls.py` is the fastest way to audit the security boundary: every
`(public)` marker there should make you ask "should it be?".

## The rules that matter

### Money is Decimal, and it is never computed on the client
Every price, fee and total is `DecimalField` server-side and quantised
ROUND_HALF_UP in `api/pricing.py` (not Python's default ROUND_HALF_EVEN, which
rounds 0.125 to 0.12 and makes a bill look wrong at a doorstep). A float cannot
represent 0.1, so a basket totalled in floats drifts.

**The checkout request carries product ids and quantities only.** No price, no
fee, no total, and the server reads none from it. `/api/store/quote` exists so
the cart drawer can show the arithmetic that will actually charge the customer.
DRF sets `COERCE_DECIMAL_TO_STRING = False` so money arrives as a JSON number —
the clients display it, they never recompute it.

### Order status is a state machine
Legal moves are declared in `Order.TRANSITIONS` and enforced by
`Order.advance_status()`, which also stamps the matching timestamp exactly once.
**Never assign `order.status` directly.** An illegal move raises `ValueError`,
which views turn into a **409** — a conflict with the order's state, not a bad
request.

```
Placed → Packing → Ready → Dispatched → Delivered
   └────────┴────────┴──────────────────→ Cancelled
                       Dispatched → Ready    (rider hands it back)
                       Dispatched → Failed   (attempted, did not happen)
```

*What is legal* is `TRANSITIONS`. *Who may ask for it* is `ADMIN_TARGETS` /
`RIDER_TARGETS` in `views/orders.py`, and both checks apply. A rider can never
cancel (that decision, and the refund conversation behind it, belongs to the
store); a manager can never dispatch (that means a specific rider physically
took the bag).

Cancelling goes through `checkout.cancel_order()`, never `advance_status` alone,
because it must also restore stock under a lock.

`Failed` is terminal and restores **nothing** — when the rider reports it the bag
is on a bike, and restocking then would list units the store cannot pick.
`checkout.restock_failed_order()` is the separate step behind
`POST /api/orders/{id}/restock`, idempotent on `restocked_at`, and it requires a
reason: that sentence is what the store reads when the customer rings.

### Checkout is one transaction, with rows locked in primary-key order
`api/checkout.py` locks product rows with `select_for_update()` ordered by id, so
two baskets containing the same products cannot deadlock. Stock check, item
insert and stock decrement land together or not at all.

### Checkout is idempotent on `Idempotency-Key`
A retried POST used to create a second order and decrement stock twice, and on
Aizawl mobile data a retry is the normal case. The key arrives as a **header**,
never a body field — the checkout body is the money boundary and stays ids and
quantities only — and is deduped by a unique constraint on
`Order.idempotency_key`. A replay answers **200**, not 201: it created nothing.

`place_order` is a thin non-atomic wrapper around `_place_order`, and the
ordering is load-bearing: the dedupe read is *outside* the transaction, or every
retry holds product locks while it looks; and the `IntegrityError` recovery is in
the wrapper, because a rolled-back atomic block cannot run another query.

### Reaching Delivered records the cash
`payment_method = "cod"` is an intention. `paid_at`, `amount_collected` and
`collected_by` are what happened, and `advance_status()` stamps the first two on
the move to Delivered — in the model, not in a view, so no route there can omit
them. `amount_collected` defaults to `grand_total`; a rider revises it down when
the customer paid short, and above the total is a 400 (that is change owed back,
not revenue).

`GET /api/analytics/cash` is the only endpoint in `views/analytics.py` bucketed
by `paid_at` rather than `created_at`, because it answers "what is in the till
tonight" and an order placed at 23:50 and delivered at 00:05 is tomorrow's cash.

### Two product serializers, on purpose
`ProductSerializer` (admin) carries cost price, supplier and shelf location.
`StoreProductSerializer` (public) carries none of them and reduces `stock` to
`in_stock` + `low_stock`. Separate classes so exposing margin data would need a
deliberate edit rather than a forgotten exclusion. The same reasoning splits
`OrderSerializer` from `OrderTrackingSerializer`.

`views/store.py` is the module-level version of that boundary: everything in it
is reachable without a token, so nothing in it may return a staff-only
serializer. Authenticated customer endpoints live in `views/customer.py` for
exactly that reason, not in `store.py`.

### Dispatch is a pull, and a reject is remembered
Every available rider in range sees every `Ready` order **except ones they have
declined**; first to accept wins, the loser gets a 409. Declines live in
`order_rejections`. The alternative — offering to one rider at a time with a
timeout — needs a scheduler and a background worker to be correct, because an
offer nobody answers has to expire and something has to expire it. The pull
design needs neither and fails honestly: the worst case is an order nobody
takes, which `GET /api/orders?stalled=true` shows the manager directly.

### Coordinates are nullable, and that is load-bearing
`Order.customer_latitude/longitude` are `null=True` with **no default**. They
used to default to the *store's own* position, so an order carrying no position
recorded the customer as standing at the counter: every rider measured 0.00 km
away, the radius filter matched everyone, and the rider app showed a confident,
false `0.0 km`. Geolocation is opt-in at checkout and declining it is a supported
outcome — `dispatch._rank` returns every rider at distance `None` for such an
order, sorted last. Distance is straight-line haversine; Aizawl is built on
ridges, so it decides whether an order is plausibly in a rider's area and is
never presented as an ETA.

### Live position is telemetry, and it never becomes authority
`api/location.py` records where a rider is (`RiderLocation`, one row per rider,
overwritten), where they went during a delivery (`OrderLocationPing`,
append-only, **written only while an order is `Dispatched`**) and where the
customer says they are waiting (`OrderCustomerLocation`, one row per order,
opt-in).

Four rules hold this together, and each exists because the obvious alternative
is worse:

- **Freshness is `received_at`, the server's clock — never `recorded_at`.** A
  handset's clock can be wrong by an hour, and staleness read from a client's
  own timestamp is staleness the client decides. `recorded_at` is stored anyway,
  because the gap between the two is how a burst replayed after a dead spot is
  told apart from a live fix.
- **A stale position is hidden from the customer and shown to the manager.**
  Past `LOCATION_STALE_SECONDS` (90) the tracking page shows nothing, because a
  marker that stopped moving reads as a rider who is nearly there. The console
  shows the last known fix with its age, because "last seen 4 minutes ago near
  Chanmari" is exactly what a manager needs and an empty map is not.
- **The customer's live position never touches `Order.customer_latitude`.**
  That column is the checkout position the radius check and dispatch were
  decided on. A fix taken later from a moving car must not be able to move where
  the bag is going.
- **None of it feeds dispatch.** `dispatch._rank` still ranks on the rider's
  static `base_latitude`. Ranking on live positions changes who gets offered
  work, so it needs its own reasoning about a rider whose phone is off — it is a
  separate decision, not a free improvement.

`GET /api/store/orders/{token}/rider-location` is a **separate route rather than
fields on `OrderTrackingSerializer`**, whose docstring promises "no rider
identity, no distances". It answers `{"rider": null}` for every reason the answer
is no — not dispatched, already delivered, never reported, gone stale — because
distinguishing them would tell a token holder when a rider's phone went dark. It
carries a point, a heading and a distance, and never a rider's id, name, phone or
`base_latitude`, which is where a member of staff lives.

`distance_km` is `None`, never `0`, for an order with no coordinates — the same
invariant as `_rank`, for the same reason.

**The trail is pruned and nothing else prunes it.** `manage.py prune_locations`
must be scheduled; see `deployment.md`. `Order.advance_status` deletes the
customer's position on any terminal move — in the model, so no route can omit
it, and it is the one write in an otherwise write-free method.

### A notification is a prompt, never the delivery mechanism
`api/push.py` wakes a rider's phone when an order is assigned or lands in the
pull feed. The rider app's fifteen-second poll remains the source of truth —
every path here is best-effort, off unless `PUSH_ENABLED`, and silent on every
failure, because it runs inside the transaction that assigns an order and must
never be able to fail it. Same contract as `api/audit.py`, for the same reason.

Two things to know before editing it. **The send is deferred to
`transaction.on_commit` and then to a daemon thread**: never buzz a phone about
an assignment a rollback undid, and never hold `select_for_update` locks across a
call to a third party. **The commit hook has its own `try`** — hooks run after the
caller returned, inline on the connection, so anything raised there is a 500 on a
request whose work already committed.

`RiderDevice.expo_token` is unique across the table, not per rider: a handset
that changes hands at shift change must belong to whoever signed in last, or one
order buzzes two riders. Notifications carry the address and the amount only —
they render on a lock screen, so the customer's name and number stay in the app.

### A customer account is optional, and an unverified one sees less
`Customer` is the third identity table (`AdminUser`, `User`, `Customer`), keyed
on the phone number `normalise_phone` already produces at checkout. **Guest
checkout is unchanged and is still the main path** — `Order.customer` is
nullable, and no token means no account. The customer comes from the token and
never from the body, exactly as the rider does; no customer id appears in any
path under `/api/customer/`.

`phone_verified_at` is the seam the design hangs on. Setting a password proves
someone *knows* a number, not that they hold the SIM, so
`views/customer.py::visible_orders` shows an unverified account only the orders
linked to it; a verified one additionally sees unclaimed orders carrying its
number — and `customer__isnull=True` inside that clause is small and
load-bearing, because Indian mobile numbers are recycled and an order already
belonging to somebody must never be matched by phone. Nothing writes the column
yet (that needs an SMS provider and DLT registration), so keep the eventual OTP
challenge **stateless** — a `TimestampSigner` token or a cache key — or the "no
migration needed" promise on the model field stops being true.

The escape hatch is the tracking token: `POST /api/customer/orders/claim`, and
`claim_token` on signup, link one order the caller can prove they hold.
**Possession of the token is the evidence, not the phone number** — it is already
the whole credential for public tracking, so claiming grants nothing new.

### Adding a fourth identity means adding a fourth throttle class
DRF's `AnonRateThrottle` returns no key once a request is authenticated, and each
per-account throttle returns none for a caller it does not recognise, so
`DEFAULT_THROTTLE_CLASSES` covers everyone exactly once only while the two sets
match. Getting it wrong is silent: nothing raises, and the new caller is simply
unmetered everywhere. It has already happened twice.

`throttle_ident` in `api/throttling.py` is the one place that knows how to name a
caller, and it namespaces by table — admin #3, rider #3 and customer #3 all exist
and are three different people, so a key built on `pk` alone merges them.
`api/tests/test_throttling.py` is the regression guard.

`NUM_PROXIES` is the other half of this. Left at DRF's default of `None`, the
throttle key for an anonymous caller is the *entire* client-supplied
`X-Forwarded-For` header, so a fresh bucket is one header away and the login,
checkout and tracking limits become decoration.

### Auth
- `api/authentication.py` answers *who is this?* and never rejects.
  `api/permissions.py` answers *may they?* and rejects.
- All three token kinds share a secret and are told apart by a `typ` claim. Each
  authentication class returns `None` — never raises — for the others' tokens,
  because DRF stops at the first class that returns a user.
- **The rider comes from the token, never the body.** `accept`/`reject`/`status`
  take no rider id and each checks ownership.
- `is_active` is re-checked on every request, so deactivating a rider revokes
  access immediately rather than when their 12-hour token expires.
- **Tokens carry `ver`, and a bump signs out every device.** `ver` is the
  account's `token_version`, compared against the row the auth class already
  reads; the logout endpoints increment it, as do password and PIN resets. A
  retired token is **401** — we no longer know who is calling — so the client
  clears its session. Per-device revocation would need a blacklist that outlives
  the token; this is deliberately coarser.
- **`ait` bounds the session, not the token.** `/api/auth/me` mints a fresh token
  on every call, so without a claim that survives refresh a stolen token renews
  itself forever. `ait` records when the session began, is copied forward
  unchanged, and `/me` refuses past `SESSION_MAX_HOURS`. Ordinary requests are
  unaffected — only renewal is capped.
- A valid token of the **wrong kind** gets **403**, not 401. 401 means "I don't
  know who you are" and is what makes the web clients clear their stored session;
  clearing it on 403 would sign an admin out of pages they merely lack rights
  for. `test_auth.py` asserts this contract.

### Two console roles, decided by the row and never by the token
`AdminUser.role` is `admin` or `manager`. A **Manager** runs the store: products,
categories, orders, riders, prices, settings, every figure in analytics. An
**Admin** adds exactly two things — `/api/admins` and `/api/audit`.

- `IsAdmin` means "an active AdminUser", i.e. either role. `IsOwnerAdmin` is the
  Admin-only guard. **Do not change `IsAdmin`.**
- **The role is not a JWT claim.** `AdminJWTAuthentication` re-reads the row on
  every request, which is what makes `is_active` an immediate revocation; the
  role inherits that, so a demotion takes effect on the next request rather than
  in twelve hours. Adding a role claim would trade that for a stale copy.
- `views/admins.py` refuses, with **409**, to let you change your own role,
  deactivate yourself, or demote the last active Admin. Without the third, one
  click leaves the console unadministrable.

### Every mutating admin view records who did it
`api/audit.py::record(...)` writes one `AuditLog` row. It never raises — an audit
failure must not fail the request that already committed — and it strips anything
named like a credential, so a PIN reset is logged as `pin_reset` and not as a
PIN.

### Operational settings are a table, commerce settings are the environment
`StoreSettings` (singleton, `pk=1`) holds opening hours, the accept-orders kill
switch, the delivery radius and the store's coordinates. Fees, thresholds and the
delivery tiers stay in environment variables.

The line is *operational vs commercial*. The first four change within a shift and
the person changing them is behind the counter, so requiring a redeploy to pause
checkout during a power cut means the shop keeps promising 15-minute delivery it
cannot make. Prices are decisions that should change with the care of a deploy.
`GET`/`PATCH /api/settings`, either console role, audited.

`StoreSettings.load()` is a plain SELECT with a `get_or_create` fallback, not
`get_or_create` outright — it is on the checkout path and on every
`/api/store/config`, and the savepoint plus INSERT attempt was measurable.

### The app refuses to boot on insecure configuration
`api/apps.py::check_production_safety()` raises outside development if
`JWT_SECRET` is the published placeholder, `ALLOWED_HOSTS` is `*` or empty,
`DJANGO_SECRET_KEY` is unset (it would fall back to `JWT_SECRET`, so one leak
would be two), `CACHE_URL` is unset (throttle counters would go in per-process
memory and every limit would be silently multiplied by the worker count — and the
login limit is what makes a 4-digit rider PIN a credential), or `CORS_ORIGINS` is
empty or `*`. Each is exploitable, not merely untidy. `test_startup.py` covers
them.

## Conventions & gotchas

- Error responses are always `{"detail": "..."}` (`api/exceptions.py`). Raise
  `NotFound`/`ValidationError`, or return `Response({"detail": ...}, status=...)`.
  Never return a bare error dict. `config/handlers.py` covers the paths DRF never
  sees, so a 404 on a mistyped URL is JSON too.
- A bad request body is **400**, not 422.
- **DRF `CharField` rejects `""`.** Optional text fields use the shared
  `OPTIONAL_TEXT` kwargs in `serializers.py`.
- **`default=` is what makes PUT replace.** `required=False` alone leaves an
  omitted field unchanged.
- URLs carry **no trailing slash** and `APPEND_SLASH = False`, because a
  redirected POST loses its body.
- Nest-heavy queries need `.prefetch_related("items")` and
  `.select_related("delivery_boy")`, or listing 50 orders is 101 queries.
- `OrderItem.product` is `on_delete=PROTECT` on purpose; the delete view counts
  references first and returns a 409 telling the caller to deactivate instead.
- Uploads return a **relative** `/uploads/<name>` path; the clients prefix it.
- Phone numbers are normalised to `+91XXXXXXXXXX` by `api/validators.py` on both
  storage and login. Two spellings of one number would otherwise be two accounts.
- Every model pins `Meta.db_table`, so the schema still matches the SQLAlchemy
  and Supabase versions it came from. Keep doing that on new models.
- `manage.py seed` deletes and reinserts **rows** only; it never touches the
  schema, but it does wipe hand-added admins.
- Migrations are source code — commit them, and write them to survive existing
  data. `0003_quick_commerce` is the worked example: it renames the old status
  vocabulary, backfills totals, dedupes category names before a unique
  constraint, and populates tracking tokens row by row before making the column
  unique (a single `AddField` with a callable default evaluates it once and gives
  every row the *same* value — for a tracking token, that means any holder could
  read every order).
- Tests swap in an MD5 password hasher (`settings.TESTING`). PBKDF2 must stay
  slow in production; it turned a 6-second suite into 53 seconds.
- `api/tests/base.py` clears the cache between tests (throttle counters are not
  in the transaction Django rolls back), opens the store around the clock (the
  default 07:00–22:00 would make every checkout test fail after ten at night),
  and returns money as `Decimal` so assertions mean what they say. Use its
  fixtures rather than building rows by hand.
- `api/schema.py` teaches drf-spectacular about the three custom auth classes.
  A new token kind needs an entry there or `/docs` will describe it as public.

## Known gaps

Cash on delivery only — no gateway, and no refunds. No token revocation list (a
leaked token is valid until it expires; deactivating the account is the
revocation path and is immediate). No background worker, so no scheduled
dispatch, no delivery-time analytics job, no email or SMS. `../PRODUCTION.md`
Part 4 is the authoritative list.
