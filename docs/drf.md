# FastAPI → Django REST Framework

You already know this API. This file re-teaches it in DRF terms, using the code
in this repository as the worked example. Everything referenced here is a real
file you can open.

Read it in order the first time. After that, the [translation
table](#the-translation-table) and [adding an endpoint](#adding-an-endpoint) are
the two sections you will keep coming back to.

---

## The one thing to internalise

FastAPI is a **routing library that reads your type hints**. You write a
function, annotate it, and the framework infers the URL, the body schema, the
validation and the docs from the signature.

Django is a **full-stack framework with an ORM at its centre**, and DRF is a
layer on top that makes it speak JSON. Nothing is inferred from a signature.
Each job has its own explicit place:

```
where the URL lives            api/urls.py          (a URLconf)
what a request is allowed      api/permissions.py   (a permission class)
who is making the request      api/authentication.py (an authentication class)
what the data must look like   api/serializers.py   (a serializer)
what the endpoint does         api/views/*.py       (a view)
what the tables look like      api/models.py        (models + migrations)
```

That is more files for the same endpoint. What you buy is that every one of
those concerns is independently replaceable and independently testable, and that
the ORM, the migration system, the admin tooling and the auth machinery are all
one ecosystem rather than five libraries you assembled.

---

## The translation table

| FastAPI | DRF | Where in this repo |
| --- | --- | --- |
| `app = FastAPI()` | `config/settings.py` + `manage.py` | there is no app object you build |
| `@router.get("/x")` above a function | `path("x", View.as_view())` in a URLconf | `api/urls.py` |
| `APIRouter(prefix=...)` | `include()` another URLconf | `config/urls.py` |
| one function per method+path | one **class** per path, one **method** per verb | `api/views/products.py` |
| `product_id: int` in the signature | `<int:product_id>` in the URL pattern | `api/urls.py` |
| `payload: ProductCreate` | `ProductSerializer(data=request.data)` | `api/views/products.py` |
| `response_model=ProductOut` | `Serializer.data` + `fields = [...]` | `api/serializers.py` |
| Pydantic `BaseModel` | `serializers.Serializer` | `LoginSerializer` |
| Pydantic model mirroring a table | `serializers.ModelSerializer` | `ProductSerializer` |
| `Depends(get_db)` | nothing — the ORM manages connections | — |
| `Depends(require_admin)` | authentication class **+** permission class | `api/authentication.py`, `api/permissions.py` |
| `APIRouter(dependencies=[...])` | `permission_classes` on a shared base class | `AdminAPIView` |
| `raise HTTPException(404, detail=...)` | `raise NotFound("...")` | `api/views/products.py` |
| automatic 422 on a bad body | `serializer.is_valid(raise_exception=True)` → 400 | everywhere |
| SQLAlchemy `select(Product)` | `Product.objects.all()` | `api/views/store.py` |
| `Base.metadata.create_all()` | `manage.py migrate` | `api/migrations/` |
| `python seed.py` | `manage.py seed` | `api/management/commands/seed.py` |
| `uvicorn app.main:app --reload` | `manage.py runserver` | — |
| `/docs` from type hints | `/docs` from drf-spectacular | `config/urls.py` |

---

## 1. Project vs app

Django has two levels, and the distinction confuses everyone once:

- **`config/`** is the *project*. Settings, the root URLconf, the WSGI/ASGI
  entry points. One per deployment.
- **`api/`** is an *app*. Models, views, serializers, migrations. A project can
  have many; this one has exactly one, because splitting a backend this size
  buys nothing.

An app is a Python package listed in `INSTALLED_APPS`, and being listed is what
makes Django discover its models, its migrations and its management commands.

`config/settings.py` is worth reading top to bottom once — it is the whole
configuration surface of the backend in one file, and it is commented with why
each block exists.

---

## 2. URLs live apart from views

This is the change you will feel most.

```python
# FastAPI — the URL is a decorator on the handler
@router.put("/{product_id}", response_model=ProductOut)
def update_product(product_id: int, payload: ProductUpdate, db=Depends(get_db)):
    ...
```

```python
# DRF — the URL is an entry in api/urls.py, pointing at a view
path("api/products/<int:product_id>", products.ProductDetailView.as_view(),
     name="product-detail")
```

```python
# api/views/products.py
class ProductDetailView(AdminAPIView):
    def put(self, request, product_id: int): ...
    def delete(self, request, product_id: int): ...
```

**Cost:** two files to keep in step. Rename the view and you must update the URL.

**Benefit:** `api/urls.py` is a complete table of contents for the API. There is
no equivalent file in a FastAPI project — you have to read every router to know
what the server answers.

### Path converters replace type-annotated path params

`<int:product_id>` does two jobs: it matches **only** digits, and it passes the
value to the view already cast to `int`.

So `/api/products/abc` does not match the pattern at all and 404s before any
code of yours runs. `product_id: int` in FastAPI gave you a 422 instead; either
way the bad input never reaches your function.

A useful side effect: the old FastAPI hazard where `GET /api/delivery/{id}`
would swallow the literal path `/api/delivery/riders` **cannot happen here**,
because `<int:>` will not match `"riders"`. Registering literal paths first is
still the tidier habit, but it is no longer load-bearing.

### `name=` and reverse lookups

Every `path()` has a `name`. `reverse("product-detail", args=[7])` returns
`/api/products/7`, so tests and redirects never hardcode URL strings. FastAPI's
`url_path_for` is the same idea, rarely used; in Django it is the norm.

---

## 3. Views: pick the right rung of the ladder

DRF gives you four levels of abstraction. **This codebase uses `APIView`
throughout**, deliberately — it is the rung where the code still reads like the
FastAPI functions it replaced, so you can diff the two in your head.

```
@api_view(["GET"])          a decorated function. Least magic.
                            -> api/views/meta.py (the health check)

APIView                     a class, one method per HTTP verb.
                            -> everything else in api/views/

generics.ListCreateAPIView  a class where you declare queryset +
                            serializer_class and inherit the body.

ModelViewSet + a router     one class for all five CRUD endpoints,
                            URLs generated for you.
```

### What `APIView` looks like

```python
class ProductListCreateView(AdminAPIView):        # AdminAPIView = APIView + IsAdmin
    def get(self, request):
        products = Product.objects.order_by("id")
        return Response(ProductSerializer(products, many=True).data)

    def post(self, request):
        serializer = ProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)
```

Three things to notice:

- **A method name is the HTTP verb.** No `get` method means DRF answers `405
  Method Not Allowed` for you.
- **`request.data`** is the parsed body — JSON, form-encoded or multipart, DRF
  negotiates it. There is no `await request.json()`.
- **`Response`** (DRF's, not Django's `HttpResponse`) takes a plain Python
  object and renders it according to what the client asked for.

### The three-line idiom

You will type this constantly. Learn it as one unit:

```python
serializer = XSerializer(data=request.data)   # 1. bind input
serializer.is_valid(raise_exception=True)     # 2. validate, or 400 and stop
serializer.save()                             # 3. INSERT or UPDATE
return Response(serializer.data)              # 4. render what was saved
```

`raise_exception=True` is what makes DRF behave like FastAPI: a bad body becomes
a 400 response and nothing after that line executes.

### Climbing to `generics` later

`ProductListCreateView` above is exactly what `ListCreateAPIView` does. The
generic version:

```python
class ProductListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdmin]
    queryset = Product.objects.order_by("id")
    serializer_class = ProductSerializer
```

Four lines instead of nine, and it is the same behaviour including the 201.
Worth doing once you are comfortable — the reason it is not written that way
here is that the explicit form shows you *what the generic is doing*, and this
backend has enough per-endpoint quirks (the 409 on product delete, the rider
conflict checks) that half the classes would need overriding anyway.

---

## 4. Serializers are Pydantic that can also write

A DRF serializer does everything a Pydantic model did:

```python
class LoginSerializer(serializers.Serializer):     # ~ class LoginRequest(BaseModel)
    email = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)
```

...and one thing Pydantic did not: **a `ModelSerializer` knows its model, so it
can perform the database write.**

```python
class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "name", "price", ...]   # <- also the response filter
        read_only_fields = ["id", "created_at"]
```

That is why `api/serializers.py` has *one* `ProductSerializer` where
`app/schemas.py` needed `ProductCreate`, `ProductUpdate` **and** `ProductOut`.

| direction | how |
| --- | --- |
| in (validate + write) | `ProductSerializer(data=request.data)` then `.save()` |
| in (update) | `ProductSerializer(instance, data=request.data)` then `.save()` |
| out (one) | `ProductSerializer(product).data` |
| out (many) | `ProductSerializer(queryset, many=True).data` |

`fields = [...]` is the security property `response_model=` gave you: an
attribute not listed cannot appear in a response. `AdminUser.password_hash` has
no serializer at all, which is stronger still.

### Where validation goes

Three layers, in the order DRF runs them:

```python
class UserSerializer(serializers.ModelSerializer):
    def validate_role(self, value):     # 1. one field
        ...
    def validate(self, attrs):          # 2. across fields (and self.instance)
        ...
```
```python
# 3. in the view — rules about the *request*, not the data
if order.status != Order.PENDING:
    return Response({"detail": "..."}, status=409)
```

The FastAPI version put all three in the route body. Moving 1 and 2 into the
serializer is the main structural improvement of this port: the parent-category
check in `CategorySerializer.validate()` now applies anywhere that serializer is
used, not just in the one route that remembered to write it.

### Two gotchas that bit this port

Both are in `api/serializers.py` with comments, but they are the kind of thing
that costs an hour if you hit them cold:

- **DRF rejects `""` by default.** `CharField` has `allow_blank=False`. The
  product editor submits `""` for every optional text box the user leaves empty,
  which Pydantic accepted happily. Hence the shared `OPTIONAL_TEXT` dict.
- **`default=` is what makes PUT replace.** A field with only `required=False`
  is *skipped* when omitted, leaving the old value. A field with an explicit
  `default=` has the default applied on a full (non-partial) update. The second
  is what `PUT` means, and what the Pydantic `ProductUpdate` model did.

---

## 5. The ORM

Django's ORM instead of SQLAlchemy. It is less powerful at the far end and much
terser at the near end, which is where 95% of this backend lives.

| SQLAlchemy 2.0 | Django |
| --- | --- |
| `db.scalars(select(Product)).all()` | `Product.objects.all()` |
| `db.get(Product, 7)` | `Product.objects.filter(pk=7).first()` |
| `select(P).where(P.status == "active")` | `Product.objects.filter(status="active")` |
| `where(func.lower(P.status) == "active")` | `Product.objects.filter(status__iexact="active")` |
| `.order_by(P.id.desc())` | `.order_by("-id")` |
| `db.scalar(select(func.count())...)` | `.count()` |
| `db.add(obj); db.commit()` | `obj.save()` or `Model.objects.create(...)` |
| `db.delete(obj); db.commit()` | `obj.delete()` |
| `lazy="selectin"` on a relationship | `.prefetch_related("items")` on the query |
| `Session` + `get_db` dependency | nothing — connections are per-request, automatic |

Two habits to carry over:

**Field lookups use `__`.** `status__iexact`, `price__gte`, `created_at__year`,
`delivery_boy__role`. Double underscore separates the field from the operator,
and it also walks relationships.

**Querysets are lazy.** `Product.objects.filter(...)` builds SQL; nothing runs
until you iterate, slice, or call `.count()`/`.first()`. You can keep chaining
filters with no cost. The flip side is the N+1: `OrderSerializer` nests `items`,
so `api/views/orders.py` uses `.prefetch_related("items")` to fetch them all in
one extra query. Without it you would get one query per order, silently.

**No session, no `get_db`.** Django opens a connection per request and closes
it, and wraps writes in a transaction when you ask (`@transaction.atomic`, used
in the seed command). The single most common `Depends(...)` in the FastAPI code
simply has no counterpart.

---

## 6. Migrations — the real upgrade

The FastAPI setup had a known hole, documented in its own README: `create_all()`
only ever creates *missing tables*. It never alters one. So changing a column
meant dropping the database, and "add a field" and "lose all your data" were the
same command.

Django ships migrations:

```bash
uv run manage.py makemigrations   # diff models.py against the last migration
uv run manage.py migrate          # apply — existing rows survive
```

`makemigrations` writes a numbered Python file into `api/migrations/`. **Commit
them.** They are the schema's version history, and `migrate` replays them in
order against any database — yours, a teammate's, production.

Useful:

```bash
uv run manage.py showmigrations           # what is applied, what is pending
uv run manage.py sqlmigrate api 0001      # print the SQL without running it
uv run manage.py migrate api 0002         # roll back to a specific migration
```

`manage.py seed` now only touches **rows**, never the schema. Those are two
different operations again, which is how it should have been all along.

---

## 7. Auth: one dependency became two classes

`require_admin` was doing two separable jobs. DRF splits them, and the split is
worth adopting mentally even outside Django:

```
authentication  "who is this?"   sets request.user.  Never rejects a request
                                 for being anonymous.
permission      "may they?"      rejects.
```

**`api/authentication.py`** — runs on every request (it is in
`DEFAULT_AUTHENTICATION_CLASSES`). Reads `Authorization: Bearer <jwt>`, decodes
it, loads the `AdminUser`, returns `(user, token)`. Returns `None` when there is
no token at all, which leaves `request.user` as `None` and lets public endpoints
serve the request.

**`api/permissions.py`** — `IsAdmin.has_permission()` returns a bool. False
becomes a 401 (because the authentication class advertises a `WWW-Authenticate`
header) or a 403 (if it did not).

Attaching the guard, both ways:

```python
# whole resource — the equivalent of APIRouter(dependencies=[Depends(require_admin)])
class ProductListCreateView(AdminAPIView):      # AdminAPIView sets permission_classes
    ...

# one endpoint — the equivalent of a per-route Depends
class OrderListView(APIView):
    permission_classes = [IsAdmin]
```

`api/views/orders.py` uses the second form on purpose: it mixes admin endpoints
and deliberately-public rider endpoints in one module, and the rider views'
*missing* `permission_classes` line is what makes them findable when rider auth
finally gets built.

The JWT itself is unchanged — same `PyJWT`, same HS256, same `sub` claim, same
`JWT_SECRET`. Tokens minted by the FastAPI backend still validate.

**Password hashes did change.** `django.contrib.auth.hashers` (PBKDF2) replaced
bcrypt, dropping a dependency and gaining transparent algorithm upgrades. Hashes
written by the old backend cannot be verified — re-run `manage.py seed`.

---

## 8. Errors

DRF exceptions map to the `{"detail": "..."}` shape the frontend reads:

```python
from rest_framework.exceptions import NotFound, ValidationError, PermissionDenied

raise NotFound("Product not found.")        # 404 {"detail": "Product not found."}
raise ValidationError("Rider not found.")   # 400
```

or return one directly when you want a status DRF has no exception for:

```python
return Response({"detail": "..."}, status=status.HTTP_409_CONFLICT)
```

The one thing DRF does *not* do out of the box is put serializer errors under
`detail` — it returns `{"name": ["This field may not be blank."]}`.
`api/exceptions.py` is a custom `EXCEPTION_HANDLER` that flattens that into
`{"detail": "name: This field may not be blank.", "errors": {...}}`, so every
error from this API has the same shape whatever produced it.

**Status code note:** a bad request body is **400** here, where FastAPI returned
**422**. The frontend only reads `detail`, so nothing broke, but it is a real
difference in the contract.

---

## 9. `manage.py` is the whole CLI

```bash
uv run manage.py runserver 8000          # dev server, auto-reloads
uv run manage.py runserver 0.0.0.0:8000  # reachable from your phone
uv run manage.py seed                    # reset sample data
uv run manage.py makemigrations          # after editing models.py
uv run manage.py migrate                 # apply migrations
uv run manage.py shell                   # REPL with Django configured
uv run manage.py check                   # config sanity check
uv run manage.py help                    # every command, including yours
```

`manage.py shell` is the tool to reach for when you want to poke at data:

```python
>>> from api.models import Product
>>> Product.objects.filter(stock=0).values_list("name", flat=True)
<QuerySet ['Onion']>
```

Adding your own command is a file in `api/management/commands/`. The filename
*is* the command name — `seed.py` becomes `manage.py seed`.

---

## Adding an endpoint

The checklist, using "list low-stock products" as the example:

**1. Serializer** (only if the shape is new) — `api/serializers.py`.
Reuse `ProductSerializer` here; nothing new needed.

**2. View** — `api/views/products.py`:

```python
from django.db.models import F   # F() lets you compare one column to another

class LowStockListView(AdminAPIView):
    def get(self, request):
        products = Product.objects.filter(stock__lte=F("reorder_level"))
        return Response(ProductSerializer(products, many=True).data)
```

**3. URL** — `api/urls.py`, literal paths above parameterised ones:

```python
path("api/products/low-stock", products.LowStockListView.as_view(), name="low-stock"),
```

**4. Model change?** Then also `makemigrations` + `migrate`.

That is it. It shows up in `/docs` automatically.

---

## What got better, and what got wordier

**Better**

- Migrations. The FastAPI setup had no schema evolution story at all.
- `api/urls.py` is a readable index of the whole API.
- Validation moved out of view bodies into serializers, where it is reusable.
- Authentication and permission are separate, nameable, testable objects.
- Uniqueness, `on_delete`, and choices are declared once on the model and
  enforced by the ORM instead of re-checked in each route.
- Timezones are handled by `USE_TZ = True`. The hand-written
  `_serialize_created_at` hack that stopped every timestamp rendering 5h30m off
  in IST is simply gone.
- One dependency fewer, and no `python-multipart`: Django parses multipart natively.

**Wordier**

- Two files (view + URLconf) per endpoint instead of one function.
- No parameter inference. `request.data`, `request.FILES`, `request.query_params`
  are read by hand rather than declared in a signature.
- `Response(Serializer(obj).data)` where FastAPI let you `return obj`.
- More framework vocabulary up front — apps, URLconfs, managers, querysets.

---

## Where to go next

1. **Refactor one view to `generics`.** `ProductListCreateView` and
   `CategoryListCreateView` are the clean candidates. You will see exactly how
   much the base class was doing.
2. **Then try a `ModelViewSet` + `DefaultRouter(trailing_slash=False)`** for
   products. `@action(detail=True, methods=["post"], url_path="assign")` maps
   onto `/api/orders/{id}/assign` neatly, which is what the orders resource
   would become.
3. **Write tests.** Django ships a test runner and DRF ships `APIClient`:

   ```python
   from rest_framework.test import APITestCase

   class ProductTests(APITestCase):
       def test_list_requires_auth(self):
           self.assertEqual(self.client.get("/api/products").status_code, 401)
   ```

   `uv add --dev pytest pytest-django` if you prefer pytest. There are no tests
   in this repo yet; the endpoints were verified by hand.
4. **Read `config/settings.py` end to end.** It is the map of every decision
   this backend makes.

## Official docs worth bookmarking

- DRF serializers — https://www.django-rest-framework.org/api-guide/serializers/
- DRF views & permissions — https://www.django-rest-framework.org/api-guide/views/
- Django queryset reference — https://docs.djangoproject.com/en/stable/ref/models/querysets/
- Django migrations — https://docs.djangoproject.com/en/stable/topics/migrations/
