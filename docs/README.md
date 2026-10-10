# docs

The deployment procedure lives at **`../deployment.md`** — one file, at the root
of this repository, covering the API's deploy to Render end to end. The
Blueprint it describes is `../render.yaml`, which is what actually runs.

The `Dockerfile` in the repository root was written for Render, for the
service that was created by hand with the Docker runtime, and it is what builds
the live API today. `render.yaml` describes the native-runtime service that
replaces it once the Blueprint is synced.

What remains here is reference material rather than procedure:

| File | What it is |
|---|---|
| `drf.md` | How this project uses Django REST Framework, and why each choice |
| `uv.md`  | Dependency management with uv — `pyproject.toml` vs `uv.lock` |
