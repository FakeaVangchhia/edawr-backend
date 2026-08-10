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

from rest_framework.views import exception_handler


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


def detail_exception_handler(exc, context):
    response = exception_handler(exc, context)

    # None means DRF does not recognise the exception — let Django's own
    # handling produce a 500 (and a traceback in the console) rather than
    # swallowing a real bug behind a tidy JSON body.
    if response is None:
        return None

    data = response.data

    if isinstance(data, dict) and isinstance(data.get("detail"), str):
        return response  # already the right shape

    response.data = {"detail": _flatten(data), "errors": data}
    return response
