"""Query-string helpers every list endpoint shares: paging and date ranges.

Every list endpoint needs the same three properties and none of them are the
default: a cap so one request cannot ask for the whole table, a floor so a
negative offset does not wrap, and tolerance of garbage input so `?limit=abc`
is a default rather than a 500. The `?from=`/`?to=` pair is here for the same
reason — three views were each turning two dates into a half-open datetime
range, and one of them was one edit away from disagreeing with the other two.

Deliberately not DRF's pagination classes. Those wrap the response in
`{"count", "next", "previous", "results"}`, and every client here — the
storefront, the admin console, the Expo app — reads a bare JSON array. Changing
that shape to gain page links nobody uses would be a breaking change for three
apps at once.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from rest_framework.exceptions import ValidationError


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


def read_choice(request, name: str, choices) -> str:
    """A query parameter that must be one of `choices`, or "" when absent.

    Validated rather than passed through. An unrecognised value used to filter
    to nothing and answer `[]` with `X-Total-Count: 0`, which is
    indistinguishable from an empty table -- so `?status=Delivred` looked like
    an empty shop rather than a typo. `choices` is any iterable of the accepted
    strings, listed in the 400 in the order given.
    """
    wanted = (request.query_params.get(name) or "").strip()
    if not wanted:
        return ""
    choices = list(choices)
    if wanted not in choices:
        raise ValidationError(
            f"Unknown {name} '{wanted}'. Expected one of: " + ", ".join(choices) + "."
        )
    return wanted


def read_date(request, name: str) -> date | None:
    """Parse `?from=YYYY-MM-DD`. Garbage is ignored, never a 500."""
    raw = (request.query_params.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def store_tz() -> ZoneInfo:
    return ZoneInfo(settings.STORE_TIMEZONE)


def span(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    """Inclusive local dates to a half-open datetime range.

    Half-open on purpose: `created_at < end`, where end is midnight *after*
    `to_date`. Using `<=` against a datetime would drop or double-count whatever
    landed in the final second, and comparing against a bare date would make the
    database cast every row and ignore the index. Local dates because Aizawl is
    UTC+5:30, and grouping by UTC date files the evening rush under tomorrow.
    """
    tz = store_tz()
    start = datetime.combine(from_date, time.min, tzinfo=tz)
    end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def filter_created_between(queryset, request):
    """Apply an optional `?from=` / `?to=` pair to `created_at`.

    Either bound may be absent. Both are inclusive local dates, applied as the
    half-open range `span` describes.
    """
    from_date = read_date(request, "from")
    if from_date:
        queryset = queryset.filter(created_at__gte=span(from_date, from_date)[0])
    to_date = read_date(request, "to")
    if to_date:
        queryset = queryset.filter(created_at__lt=span(to_date, to_date)[1])
    return queryset
