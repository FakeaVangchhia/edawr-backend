"""JSON error pages, so a failure outside DRF keeps the API's contract.

`api/exceptions.py` guarantees `{"detail": "..."}` for everything DRF raises.
It deliberately does *not* handle exceptions DRF does not recognise -- those
should surface as a real 500 with a traceback rather than be swallowed into a
tidy body. But "surface as a 500" then meant Django's stock **HTML** error page,
and three clients read `data.detail` off every failure. So a genuine bug arrived
at the storefront as an unparseable response, and the customer saw the generic
"Something went wrong" instead of anything specific.

A URL that matches no pattern has the same problem: `/api/prodcts` returned an
HTML 404, which is the one shape the clients cannot read.

These are wired in `config/urls.py`. Django looks them up by the module-level
names `handler400`/`handler403`/`handler404`/`handler500`, and only uses them
when `DEBUG` is off -- in development you still get the traceback page, which is
what you want there.
"""

import logging

from django.http import JsonResponse

logger = logging.getLogger("api")


def _json(detail: str, status: int) -> JsonResponse:
    return JsonResponse({"detail": detail}, status=status)


def bad_request(request, exception=None):
    return _json("Malformed request.", 400)


def permission_denied(request, exception=None):
    return _json("You do not have permission to perform this action.", 403)


def not_found(request, exception=None):
    return _json("Not found.", 404)


def server_error(request):
    # Django has already logged the traceback through `django.request`, which
    # settings.py pins at ERROR precisely so this is never silent. The message
    # is deliberately opaque: an exception string can carry a table name, a
    # query, or a fragment of someone's address.
    return _json("Something went wrong on our end.", 500)
