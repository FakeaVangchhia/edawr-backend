"""App configuration and startup checks.

Every Django app has one of these. It is also the officially supported startup
hook: `ready()` runs once, after settings are loaded and the model registry is
populated, which makes it the equivalent of FastAPI's `lifespan` handler.

`check_production_safety()` is the important part. Each item it refuses to boot
on is a setting whose *development* default is actively dangerous in production
— not a style preference. The rule for adding one: if shipping the default
would let a stranger read or forge something, it belongs here; if it would just
be slow or untidy, it does not.
"""

import os
from pathlib import Path

from django.apps import AppConfig
from django.conf import settings


def check_production_safety() -> list[str]:
    """Refuse to boot outside development with insecure configuration.

    In development it returns warnings to print; anywhere else it raises,
    because every one of these is exploitable rather than merely untidy.
    """
    problems: list[str] = []

    if settings.JWT_SECRET == settings.INSECURE_DEFAULT_JWT_SECRET:
        problems.append(
            "JWT_SECRET is still the placeholder from .env.example, which is "
            "published in this repository — anyone who knows an admin email "
            "could forge an admin token. Generate one with:\n"
            '    uv run python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )

    if not settings.IS_DEVELOPMENT:
        if "*" in settings.ALLOWED_HOSTS:
            problems.append(
                "ALLOWED_HOSTS is '*'. List your real hostnames instead, or the "
                "app will answer to any Host header sent to it."
            )

        if not settings.ALLOWED_HOSTS:
            problems.append("ALLOWED_HOSTS is empty. Set it to your API hostname.")

        # Checked against the environment rather than settings.SECRET_KEY,
        # because settings.py already substituted the derived fallback and the
        # resulting value is indistinguishable from a real one.
        if not os.environ.get("DJANGO_SECRET_KEY", "").strip():
            problems.append(
                "DJANGO_SECRET_KEY is unset, so it is being derived from "
                "JWT_SECRET. Set it to its own random value — one secret "
                "signing two different things means a leak in either is a leak "
                "in both."
            )

        if not settings.CACHE_URL:
            problems.append(
                "CACHE_URL is unset, so throttle counters live in per-process "
                "memory. With N workers every rate limit is silently N times "
                "looser, and the login limit is what makes a 4-digit rider PIN "
                "a credential. Point it at Redis: CACHE_URL=redis://host:6379/0"
            )

        if not settings.CORS_ALLOWED_ORIGINS:
            problems.append(
                "CORS_ORIGINS is empty, so the browser will block every request "
                "from your frontend. Set it to the storefront's origin."
            )

        if "*" in settings.CORS_ALLOWED_ORIGINS:
            problems.append(
                "CORS_ORIGINS contains '*'. Name the exact origins instead."
            )

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
        # Importing the module is what registers the OpenAPI security schemes
        # for the two authentication classes. Nothing else uses the name.
        from api import schema  # noqa: F401

        for warning in check_production_safety():
            print(f"\n  WARNING: {warning}\n")

        Path(settings.MEDIA_ROOT).mkdir(parents=True, exist_ok=True)
