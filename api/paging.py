"""Limit/offset paging, clamped.

Every list endpoint needs the same three properties and none of them are the
default: a cap so one request cannot ask for the whole table, a floor so a
negative offset does not wrap, and tolerance of garbage input so `?limit=abc`
is a default rather than a 500.

Deliberately not DRF's pagination classes. Those wrap the response in
`{"count", "next", "previous", "results"}`, and every client here — the
storefront, the admin console, the Expo app — reads a bare JSON array. Changing
that shape to gain page links nobody uses would be a breaking change for three
apps at once.
"""

from __future__ import annotations

from django.conf import settings


def read_page(request, *, default: int | None = None, maximum: int | None = None) -> tuple[int, int]:
    """Return a clamped `(limit, offset)` from the query string."""
    default = settings.STORE_PAGE_SIZE if default is None else default
    maximum = settings.STORE_MAX_PAGE_SIZE if maximum is None else maximum

    def read(name: str, fallback: int) -> int:
        try:
            return int(request.query_params.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    limit = max(1, min(read("limit", default), maximum))
    offset = max(0, read("offset", 0))
    return limit, offset
