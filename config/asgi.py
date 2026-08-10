"""ASGI entry point — the async equivalent of `wsgi.py`.

    uv run uvicorn config.asgi:application --port 8000

Kept because it is one line and because it is the door to websockets later (the
frontend already has a socket.io hook waiting for a server). Every view in this
project is synchronous, so plain WSGI is the simpler default for now.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()
