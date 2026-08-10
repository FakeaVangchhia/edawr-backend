# Dependency management with uv

This backend uses [uv](https://docs.astral.sh/uv/) instead of pip. This file
explains what changed, why, what each file does, and every command you need.

---

## What changed

| Before (pip) | Now (uv) |
| --- | --- |
| `requirements.txt` — flat list of pinned versions | `pyproject.toml` — declared dependencies |
| *(nothing)* | `uv.lock` — exact resolved versions of the whole tree |
| *(nothing)* | `.python-version` — the Python version this project uses |
| `python -m venv .venv` | `uv sync` (creates `.venv` for you) |
| `.venv/Scripts/activate` then `python x.py` | `uv run python x.py` (no activation) |
| `pip install -r requirements.txt` | `uv sync` |

`requirements.txt` and the hand-built `.venv` were deleted. `uv sync` recreates
an equivalent environment — the same 18 packages, same versions.

---

## Why

**1. `requirements.txt` couldn't tell two things apart.** It listed
`django==6.1` next to nothing about `asgiref`, `sqlparse`, `tzdata` — the
packages Django itself depends on. So either you pinned only your direct
dependencies (and everyone's transitive tree drifted), or you froze all 18 (and
lost track of which 7 you actually chose). `pyproject.toml` holds the 7 you
chose; `uv.lock` holds all 18 as resolved.

**2. Reproducibility.** `uv.lock` records exact versions *and* file hashes for
every package on every platform. `uv sync` reproduces that tree byte-for-byte.
`pip install -r requirements.txt` could and did produce different trees on
different machines and different days.

**3. Speed.** uv is written in Rust and caches aggressively. A warm `uv sync`
here takes well under a second; the original `pip install` took roughly 30.

**4. No activation step.** `uv run` finds `.venv` and uses it. You can't forget
to activate, and you can't accidentally install into your system Python.

**5. It manages Python itself.** `.python-version` says `3.14`. If your machine
doesn't have 3.14, uv downloads it. No pyenv, no manual installs.

---

## The files

### `pyproject.toml` — what this project needs

The standard Python project file (PEP 621), not a uv invention. The part you'll
edit:

```toml
dependencies = [
    "django>=5.2.17",
    "djangorestframework>=3.18.0",
    ...
]
```

**Only direct dependencies go here** — the things `api/` and `config/` actually
import. `asgiref` is not listed even though Django is built on it, because we
don't import it. uv resolves those.

**Lower bounds (`>=`), not exact pins (`==`).** The split matters:

- `pyproject.toml` says what is **compatible** — "any django from 5.2.17 up"
- `uv.lock` says what is **installed** — "django 6.1 exactly, hash abc123"

Pinning `==` in both places means you can never take a bugfix release without
editing two files. Ranges in `pyproject.toml` plus a lockfile is the shape that
lets you upgrade deliberately (`uv lock --upgrade`) while everyone else stays
on the exact tree you tested.

Square brackets after a name are an **extra** — an optional feature set the
package declares. Nothing here uses one yet; you will the moment you move to
Postgres, where `uv add "psycopg[binary]"` pulls in the pre-compiled C driver
instead of expecting you to have libpq and a compiler.

```toml
[tool.uv]
package = false
```

This backend is an *application*, not a library — nothing does
`import edawr_backend`. `package = false` tells uv to install the dependencies
but not to build and install this project itself. Without it, uv would try to
build a wheel out of `backend/` and fail or waste time. Set it to `true` (or
delete the line) only if you ever publish this as an importable package.

### `uv.lock` — what is actually installed

Auto-generated. **Never edit it by hand, and always commit it.** It is what
makes your machine, a teammate's machine, and production identical. Roughly:

```toml
[[package]]
name = "django"
version = "6.1"
dependencies = [ { name = "asgiref" }, { name = "sqlparse" }, ... ]
```

Committing lockfiles is right for applications. Libraries usually don't, because
their consumers do the resolving.

### `.python-version` — which Python

Contains `3.14`. `uv run` and `uv sync` read it and use that version,
downloading it if missing. Also understood by pyenv, so it's not uv-specific.

### `.venv/` — the environment

Created and managed by `uv sync`. Gitignored. You never activate it manually and
never `pip install` into it — if you do, the next `uv sync` will undo your
change, which is the point.

---

## Commands

### Daily

```bash
cd backend

uv sync                                       # make .venv match uv.lock
uv run manage.py migrate                      # create/update the schema
uv run manage.py seed                         # load sample data
uv run manage.py runserver 8000
```

`uv run <anything>` executes inside the project environment. No activation.
`uv run manage.py <cmd>` is the shape you will type most — see
[docs/drf.md](drf.md) for the full command list.
`uv sync` is fast and idempotent — running it when nothing changed is a no-op,
so run it freely after pulling.

### Managing dependencies

```bash
uv add httpx                    # add a dependency
uv add --dev pytest             # add a dev-only dependency
uv remove httpx                 # remove one
```

`uv add` edits `pyproject.toml`, re-resolves, updates `uv.lock`, and installs —
one command, all four steps. Don't hand-edit `pyproject.toml` and hope; use
`uv add` so the lockfile stays in sync.

### Upgrading

```bash
uv lock --upgrade               # re-resolve everything to newest compatible
uv lock --upgrade-package django    # just one
uv sync                         # apply the new lock to .venv
```

Deliberate and reviewable: `uv lock --upgrade` changes `uv.lock`, so the diff
shows exactly what moved before you install it.

### Inspecting

```bash
uv tree                         # dependency tree, shows why a package is there
uv pip list                     # flat list of installed packages
uv run python -c "import django; print(django.__version__)"
```

### Production

```bash
uv sync --frozen --no-dev
```

- `--frozen` — fail if `uv.lock` is out of date rather than silently
  re-resolving. You want a deploy to error, not quietly install something else.
- `--no-dev` — skip dev dependencies.

---

## Getting uv

Already installed here (0.11.30). On a fresh machine:

```bash
# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Troubleshooting

**`uv: command not found`** — installed but not on `PATH`. It lands in
`~/.local/bin`; restart your shell.

**A package seems missing after editing `pyproject.toml` by hand** — run
`uv sync`. Editing the file doesn't install anything on its own. Prefer
`uv add`.

**`uv.lock` conflicts in a merge** — don't hand-resolve it. Take either side,
then run `uv lock` to regenerate.

**Something needs a real `requirements.txt`** (an old CI image, a PaaS that only
speaks pip) — export one instead of maintaining it:

```bash
uv export --no-dev --format requirements-txt > requirements.txt
```

Treat it as a build artifact, not a source file. `pyproject.toml` and `uv.lock`
remain the source of truth.

**Want the old activate-then-run workflow** — it still works; `.venv` is an
ordinary virtualenv:

```bash
.venv/Scripts/activate     # macOS/Linux: source .venv/bin/activate
python manage.py runserver
```

But use `uv run` — one less step and one less way to be in the wrong
environment.
