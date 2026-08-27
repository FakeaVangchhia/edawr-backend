# backend/docs

The deployment procedure lives at **`../deployment.md`** — one file, at the root
of this repository, covering the API's deploy to Google Cloud Run end to end.

It was moved out of this directory and back again. The intermediate step was a
`PRODUCTION.md` in the containing folder that was to cover all four packages at
once; that folder is not a git repository, so the document was untracked, and it
is now gone. Anything here that still points at `PRODUCTION.md` is a stale
reference, not a file you have failed to find.

What remains here is reference material rather than procedure:

| File | What it is |
|---|---|
| `drf.md` | How this project uses Django REST Framework, and why each choice |
| `uv.md`  | Dependency management with uv — `pyproject.toml` vs `uv.lock` |
