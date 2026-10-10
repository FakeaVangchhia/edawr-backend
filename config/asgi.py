"""ASGI entry point — the async equivalent of `wsgi.py`.

Nothing runs it: every view is synchronous and gunicorn serves `wsgi.py` (see
`config/gunicorn.py`). It stays because it is one line and it is the door to
websockets later; an ASGI server (`uvicorn`, say) would have to be added to
`pyproject.toml` first.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()
