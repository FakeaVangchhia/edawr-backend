#!/usr/bin/env python
"""Django's command-line entry point.

This one file replaces the `uvicorn app.main:app` command *and* `seed.py` *and*
any future schema-migration tool. Everything you run against the backend now
goes through it:

    uv run manage.py runserver 8000     start the dev server (auto-reloads)
    uv run manage.py seed               drop + recreate the sample data
    uv run manage.py makemigrations     write a migration for model changes
    uv run manage.py migrate            apply migrations to the database
    uv run manage.py shell              a REPL with Django already configured
    uv run manage.py help               list every available command

`DJANGO_SETTINGS_MODULE` is the one piece of global state Django needs: it tells
`django.setup()` which settings file to import. Every command below runs with
`config.settings` loaded, which is why models and settings can be imported
freely from anywhere once a command has started.
"""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover - only hit on a broken env
        raise ImportError(
            "Couldn't import Django. Run `uv sync` in backend/ first."
        ) from exc

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
