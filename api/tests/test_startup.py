"""Tests for `check_production_safety()` — the boot-time refusal.

This function is the last thing standing between a careless deploy and a
production service running on development defaults. It had no tests at all,
which is a strange place for a codebase this careful to leave uncovered: it is
the one function whose *failure to fail* is the whole problem, and nothing was
asserting that it fails.

Each test drives one branch by overriding the setting it guards and asserting
the message names it. They are string checks on purpose — the message is the
product here. Someone reads it at 2am with a broken deploy, and a `RuntimeError`
that says only "insecure configuration" would send them to the source.
"""

from __future__ import annotations

import contextlib
import warnings
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from api.apps import check_production_safety

POSTGRES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "edawr"}}
SQLITE = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "edawr.db"}}

# A configuration with nothing wrong with it. Each test below breaks exactly one
# thing, so a failure names the branch rather than the fixture.
SAFE = {
    "ENVIRONMENT": "production",
    "IS_DEVELOPMENT": False,
    "JWT_SECRET": "a-real-secret-that-is-not-the-placeholder",
    "ALLOWED_HOSTS": ["api.edawr.example"],
    "CORS_ALLOWED_ORIGINS": ["https://edawr.example"],
    "CACHE_URL": "redis://localhost:6379/0",
    "DATABASES": POSTGRES,
}


@contextlib.contextmanager
def settings_of(**overrides):
    """`override_settings`, minus the DATABASES warning.

    Django warns that overriding DATABASES "can lead to unexpected behavior",
    because an already-open connection keeps the old configuration. That is a
    real hazard for a test that queries, and irrelevant here: these tests are
    `SimpleTestCase` and `check_production_safety()` only reads the engine name
    out of the dict — nothing connects.

    Silenced rather than tolerated because four spurious warnings per run is how
    a suite teaches people to skim its output.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Overriding setting DATABASES", category=UserWarning
        )
        with override_settings(**{**SAFE, **overrides}):
            yield


class ProductionSafetyTests(SimpleTestCase):
    """Outside development, every one of these must refuse to boot."""

    def assertRefuses(self, expected: str, **overrides):
        """Assert the check raises, and that the message names the cause."""
        with settings_of(**overrides), self.env_secret_key():
            with self.assertRaises(RuntimeError) as caught:
                check_production_safety()
        self.assertIn(expected, str(caught.exception))

    def env_secret_key(self, value: str = "a-real-django-secret"):
        """DJANGO_SECRET_KEY is read from os.environ, not from settings.

        `settings.py` has already substituted its derived fallback by the time
        anything can read `settings.SECRET_KEY`, and the result is
        indistinguishable from a real value — which is why the check itself
        looks at the environment, and why the tests have to as well.
        """
        return patch.dict("os.environ", {"DJANGO_SECRET_KEY": value})

    # --- the branch this file was added for -------------------------------
    def test_sqlite_is_refused_in_production(self):
        """The database check: SQLite silently drops the stock lock.

        `select_for_update()` in api/checkout.py is a no-op on SQLite, so the
        guard against selling the last unit twice does not exist there. Nothing
        about that is visible at boot, which is exactly why it is checked here.
        """
        self.assertRefuses("SQLite", DATABASES=SQLITE)

    def test_postgres_is_accepted(self):
        with settings_of(), self.env_secret_key():
            self.assertEqual(check_production_safety(), [])

    # --- the branches that existed but were never covered ------------------
    def test_placeholder_jwt_secret_is_refused(self):
        from django.conf import settings

        self.assertRefuses(
            "JWT_SECRET", JWT_SECRET=settings.INSECURE_DEFAULT_JWT_SECRET
        )

    def test_wildcard_allowed_hosts_is_refused(self):
        self.assertRefuses("ALLOWED_HOSTS", ALLOWED_HOSTS=["*"])

    def test_empty_allowed_hosts_is_refused(self):
        self.assertRefuses("ALLOWED_HOSTS", ALLOWED_HOSTS=[])

    def test_missing_cache_url_is_refused(self):
        self.assertRefuses("CACHE_URL", CACHE_URL="")

    def test_wildcard_cors_is_refused(self):
        self.assertRefuses("CORS_ORIGINS", CORS_ALLOWED_ORIGINS=["*"])

    def test_empty_cors_is_refused(self):
        self.assertRefuses("CORS_ORIGINS", CORS_ALLOWED_ORIGINS=[])

    def test_missing_django_secret_key_is_refused(self):
        with settings_of(), self.env_secret_key(""):
            with self.assertRaises(RuntimeError) as caught:
                check_production_safety()
        self.assertIn("DJANGO_SECRET_KEY", str(caught.exception))

    # --- development is the permissive case, deliberately ------------------
    def test_development_warns_instead_of_raising(self):
        """In development these are returned to be printed, not raised.

        A developer running `runserver` on SQLite with the placeholder secret is
        doing the intended thing; refusing to boot there would make the project
        impossible to clone and run, which is what the defaults exist for.
        """
        from django.conf import settings

        with settings_of(
            ENVIRONMENT="development",
            IS_DEVELOPMENT=True,
            JWT_SECRET=settings.INSECURE_DEFAULT_JWT_SECRET,
            ALLOWED_HOSTS=["*"],
            DATABASES=SQLITE,
            CACHE_URL="",
        ):
            problems = check_production_safety()

        self.assertTrue(problems)
        # Only the JWT placeholder is checked in development — the rest are
        # guarded by `if not IS_DEVELOPMENT`, and SQLite is the local default.
        self.assertEqual(len(problems), 1)
        self.assertIn("JWT_SECRET", problems[0])


class ThrottleIdentityTests(SimpleTestCase):
    """NUM_PROXIES must be set, or no rate limit in this project exists.

    DRF's `BaseThrottle.get_ident()` falls back to using the whole
    `X-Forwarded-For` header as the throttle key when `NUM_PROXIES` is None.
    That header is supplied by the caller, so an attacker varies it and gets a
    fresh bucket per request — defeating the login limit that is the only thing
    making a 4-digit rider PIN a credential.

    This asserts the setting rather than the behaviour because the behaviour
    lives in DRF; what this project can get wrong is dropping the key.
    """

    def test_num_proxies_is_configured(self):
        from django.conf import settings

        self.assertIsNotNone(
            settings.REST_FRAMEWORK.get("NUM_PROXIES"),
            "NUM_PROXIES is unset, so DRF keys every throttle on the raw "
            "X-Forwarded-For header and all four rate limits become "
            "decoration. See the comment beside it in config/settings.py.",
        )
