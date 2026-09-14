# docs

The deployment procedure lives at **`../deployment.md`** — one file, at the root
of this repository, covering the API's deploy to Render end to end. The
Blueprint it describes is `../render.yaml`, which is what actually runs.

It was moved out of this directory and back again. The intermediate step was a
`PRODUCTION.md` in the containing folder that was to cover all four packages at
once; that folder is not a git repository, so the document was untracked, and it
is now gone. Anything here that still points at `PRODUCTION.md` is a stale
reference, not a file you have failed to find.

The platform moved too: an earlier version of `deployment.md` deployed to Google
Cloud Run. That is gone, so a `gcloud` command, or a Cloud Storage bucket, is
stale in the same way. The `Dockerfile` in the repository root is *not* a
leftover from that era: it was written for Render, for the service that was
created by hand with the Docker runtime, and it is what builds the live API
today. `render.yaml` describes the native-runtime service that replaces it once
the Blueprint is synced.

What remains here is reference material rather than procedure:

| File | What it is |
|---|---|
| `drf.md` | How this project uses Django REST Framework, and why each choice |
| `uv.md`  | Dependency management with uv — `pyproject.toml` vs `uv.lock` |
