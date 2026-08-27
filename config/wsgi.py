"""WSGI entry point — what a production server imports.

`manage.py runserver` imports this too, but wraps it in an auto-reloader and
prints a banner telling you not to serve with it. That banner is correct, and
the two things below are the other half of the sentence.

**Production** is the container, and it serves with gunicorn:

    gunicorn --config config/gunicorn.py

The worker model, the timeouts and what is trusted from the proxy are all
decided in `config/gunicorn.py`, which is the file to read before changing any
of them. The Dockerfile's CMD is exactly the line above. See `deployment.md`.

**On Windows** gunicorn will not start at all — it needs `fork` and `fcntl`.
waitress is in the dev dependency group for that case, and is enough to check
that something behaves the same way without the auto-reloader in front of it:

    uv run waitress-serve --listen=127.0.0.1:8000 --threads=8 config.wsgi:application

It is a local check, not a second production target: nothing deploys waitress,
and it reads none of `config/gunicorn.py`.

WSGI is the synchronous Python web server interface; see `asgi.py` for the
async one, and the top of `config/gunicorn.py` for why this project is on WSGI.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
