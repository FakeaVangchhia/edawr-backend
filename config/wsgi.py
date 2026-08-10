"""WSGI entry point — what a production server imports.

    uv run gunicorn config.wsgi:application        (Linux/macOS)
    uv run waitress-serve --port=8000 config.wsgi:application   (Windows)

`manage.py runserver` uses this too, but wraps it in an auto-reloader. WSGI is
the synchronous Python web server interface; see `asgi.py` for the async one.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
