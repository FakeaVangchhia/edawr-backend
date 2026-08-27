"""One error shape for the whole API: `{"detail": "..."}`.

The frontend reads `data.detail || 'Failed to save'` everywhere, so every error
this API can produce has to put a human-readable string under `detail`.

DRF's built-in handler already does that for `APIException` subclasses
(`NotFound`, `PermissionDenied`, `AuthenticationFailed`, ...). The exception is
`ValidationError`, which returns a field-keyed structure instead:

    {"name": ["This field may not be blank."], "role": ["..."]}

Useful for a form, useless to a client that only looks at `detail`. This handler
flattens that into one sentence and keeps the original structure alongside it
under `errors`, so a future field-level UI has something to work with.
"""

import logging

from rest_framework.exceptions import APIException, Throttled
from rest_framework.views import exception_handler

logger = logging.getLogger("api.throttle")


class Conflict(APIException):
    """409 — the request is well-formed but disagrees with the row's state.

    The distinction this project draws, and draws consistently:

        400  you sent something wrong
        409  you sent something fine, and the world is not in a state for it

    `Order.advance_status()` already produces the second kind by raising
    `ValueError`, which the order views turn into a 409. Cancellation is the
    same class of condition — "this order has already left the store" is not a
    malformed body — but it used to raise DRF's `ValidationError` and come back
    as a 400. Two status codes for one meaning is the sort of thing a client
    handles right once and wrong forever after.
    """

    status_code = 409
    default_detail = "That conflicts with the current state of this record."
    default_code = "conflict"


class StoreClosed(APIException):
    """503 — the shop is shut, or a manager has paused new orders.

    Deliberately **not** a 409. Checkout already answers 409 for "some of these
    items are no longer available", and the storefront reads that status to grey
    out the offending cart rows. Reusing it for a closed store would make the
    cart mark every line unavailable, which is both wrong and alarming.

    503 is also the honest code: the condition is temporary and the client
    should try later, which is exactly what "we open at 07:00" means.
    """

    status_code = 503
    default_detail = "The store is not accepting orders right now."
    default_code = "store_closed"


def _flatten(data, prefix: str = "") -> str:
    """Turn DRF's nested error structure into one readable sentence."""
    if isinstance(data, dict):
        parts = []
        for key, value in data.items():
            # `non_field_errors` is DRF's key for object-level errors raised by
            # `Serializer.validate()`. Prefixing with it would be noise.
            label = "" if key == "non_field_errors" else str(key)
            parts.append(_flatten(value, label))
        return " ".join(part for part in parts if part)

    if isinstance(data, (list, tuple)):
        joined = " ".join(_flatten(item) for item in data)
    else:
        joined = str(data)

    return f"{prefix}: {joined}" if prefix else joined


def _log_throttle(exc: Throttled, context) -> None:
    """One line whenever a rate limit actually stops someone.

    The limits in `DEFAULT_THROTTLE_RATES` were carefully chosen and completely
    unobservable: nothing recorded a trip, so there was no way to tell a store
    whose riders share one carrier NAT and keep exhausting the login budget from
    a store nobody is attacking — and no way to know a limit was set wrong until
    a manager rang to say the console had stopped working.

    Deliberately not an alert and not a counter. A throttle trip is ordinary on
    a public endpoint; what makes it worth a log line is the *pattern*, and a
    pattern is something an aggregator finds in structured logs.

    `WARNING`, so it stands out from the request stream without implying a fault
    — the limit doing its job is the system working.
    """
    view = context.get("view")
    request = context.get("request")

    logger.warning(
        "throttled",
        extra={
            # `throttle_scope` for the scoped limits (login, checkout, tracking,
            # reports); the class name is the best available label for the anon
            # and staff backstops, which have no scope attribute.
            "scope": getattr(view, "throttle_scope", "")
            or type(view).__name__,
            "path": getattr(request, "path", ""),
            "method": getattr(request, "method", ""),
            # Whether a *credential* was throttled. An anonymous trip is one
            # address; an authenticated one means a specific account is hammering
            # the API, which is a different conversation entirely.
            "authenticated": bool(getattr(request, "user", None)),
            "retry_after": exc.wait,
        },
    )


def detail_exception_handler(exc, context):
    response = exception_handler(exc, context)

    # None means DRF does not recognise the exception — let Django's own
    # handling produce a 500 (and a traceback in the console) rather than
    # swallowing a real bug behind a tidy JSON body.
    if response is None:
        return None

    # After the handler ran, so `Retry-After` is already on the response and the
    # log records the same number the client was told to wait.
    if isinstance(exc, Throttled):
        _log_throttle(exc, context)

    data = response.data

    if isinstance(data, dict) and isinstance(data.get("detail"), str):
        return response  # already the right shape

    response.data = {"detail": _flatten(data), "errors": data}
    return response
