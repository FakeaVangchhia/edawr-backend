"""App configuration and startup checks.

Every Django app has one of these. It is also the officially supported startup
hook: `ready()` runs once, after settings are loaded and the model registry is
populated, which makes it the equivalent of FastAPI's `lifespan` handler.

What `lifespan` used to do, and where each piece went:
  - print/raise on insecure config  -> `check_production_safety()`, below
  - `Base.metadata.create_all()`    -> gone; `manage.py migrate` owns the schema
  - `mkdir(upload_dir)`             -> still here
"""

from pathlib import Path

from django.apps import AppConfig
from django.conf import settings


def check_production_safety() -> list[str]:
    """Refuse to boot outside development with insecure defaults.

    In development it returns warnings to print; anywhere else it raises,
    because a deployment signing tokens with a secret published in this
    repository is trivially forgeable.
    """
    problems: list[str] = []

    if settings.JWT_SECRET == settings.INSECURE_DEFAULT_JWT_SECRET:
        problems.append(
            "JWT_SECRET is still the placeholder from .env.example. Generate one with:\n"
            '    uv run python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )

    if not settings.IS_DEVELOPMENT and "*" in settings.ALLOWED_HOSTS:
        problems.append("ALLOWED_HOSTS is '*'. List your real hostnames instead.")

    if not problems:
        return []

    if settings.IS_DEVELOPMENT:
        return problems

    raise RuntimeError(
        "Refusing to start with ENVIRONMENT=%s and insecure configuration:\n  - %s"
        % (settings.ENVIRONMENT, "\n  - ".join(problems))
    )


class ApiConfig(AppConfig):
    name = "api"
    verbose_name = "eDawr API"

    def ready(self) -> None:
        # Importing the module is what registers the OpenAPI security scheme
        # for AdminJWTAuthentication. Nothing else uses the name.
        from api import schema  # noqa: F401

        for warning in check_production_safety():
            print(f"\n  WARNING: {warning}\n")

        Path(settings.MEDIA_ROOT).mkdir(parents=True, exist_ok=True)
