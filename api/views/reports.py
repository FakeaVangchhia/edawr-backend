"""Where the clients send their failures.

Until this file existed, a crash in the storefront, the console or the rider app
went nowhere at all. `frontend/src/app/error.tsx` said so in its own docblock:
there was no endpoint to report to, and `proxy.ts` would have blocked a request
to a third-party collector anyway. So the only way anybody learned that a
console screen was throwing was a phone call from the shop.

**Same-origin on purpose, not for want of Sentry.** Both frontends build their
CSP's `connect-src` from `NEXT_PUBLIC_API_URL` and allow nothing else, so a
third-party collector means widening the CSP in two packages and taking a
dependency that does nothing until somebody pays for it and pastes in a DSN. The
API is already an allowed origin. Reports land here, get logged as one JSON
object per line by `config.logformat.JsonFormatter`, and Cloud Logging indexes
them with everything else — no new infrastructure, and it works on day one.

**Both endpoints are public, and that is the interesting part.** A crash report
is worth having precisely when nobody is signed in, and a CSP violation is sent
by the browser itself with no way to attach a token. Public and unauthenticated
means anyone can post here, so:

  - Both are throttled under the `reports` scope.
  - Every field is **allowlisted**, length-capped and coerced to a string. A
    handler that logged whatever arrived would be a PII sink that any passer-by
    could fill, and log storage somebody else can write to without limit is a
    denial-of-wallet as much as a privacy problem.
  - Nothing here reads or writes the database. A report costs a log line.

The response is always 204. There is nothing useful to tell a client that has
just crashed, and an error path that can itself fail is an error path that gets
retried in a loop.
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger("api.reports")


class CspReportParser(JSONParser):
    """JSON, under the content type the `report-uri` directive posts.

    The body is JSON, but the browser labels it `application/csp-report` and DRF
    matches parsers on the media type exactly — so without this the report is
    refused with a 415 and the endpoint is decoration. Nothing here overrides
    parsing; only the label it answers to.
    """

    media_type = "application/csp-report"


class ReportingApiParser(JSONParser):
    """The same, for the newer Reporting API's `application/reports+json`."""

    media_type = "application/reports+json"

# Longest string any single field may contribute to a log line. A stack trace is
# the only field that legitimately approaches it; everything else is far under.
MAX_FIELD = 2000
MAX_STACK = 8000


def clean(value, limit: int = MAX_FIELD) -> str:
    """One untrusted value, made safe to log.

    Coerced to `str` because the body is JSON from an anonymous caller and may
    hold a nested object, a list, or a number where a string was expected — and
    a log formatter that raises on an unexpected type turns a crash report into
    a second crash. Truncated because the cap is the only thing standing between
    a public endpoint and unbounded log storage.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\x00", "")
    return text[:limit]


class ClientErrorView(APIView):
    """POST /api/client-errors — an unhandled error in one of the three clients.

    Called from the Next.js `error.tsx` boundaries and the rider app's error
    boundary. The allowlist below is the whole contract; a client that sends
    more gets the rest ignored, and one that sends less gets empty strings.
    """

    throttle_scope = "reports"

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}

        logger.warning(
            "client error",
            extra={
                # Which app, so a storefront crash and a console crash are not
                # one undifferentiated pile.
                "client": clean(body.get("client"), 32),
                "release": clean(body.get("release"), 64),
                "route": clean(body.get("route"), 200),
                # `error_message`, not `message`. Python's logging module
                # reserves `message` on a LogRecord and raises a KeyError if
                # `extra` tries to set it — which would turn every crash report
                # into a 500, on the one endpoint whose entire job is to work
                # when things are already going wrong.
                "error_message": clean(body.get("message")),
                # Next.js hands the boundary a `digest` and nothing else for an
                # error thrown during server rendering — the message is withheld
                # from the browser on purpose. The digest is what ties this
                # report to the full traceback already in the server log.
                "digest": clean(body.get("digest"), 64),
                "stack": clean(body.get("stack"), MAX_STACK),
                "user_agent": clean(request.headers.get("User-Agent"), 200),
            },
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class CspReportView(APIView):
    """POST /api/csp-report — the browser refused to load something.

    Worth having because the storefront's CSP is strict enough to break the site
    if it is wrong, and it can only be wrong in production: `script-src` uses
    `'strict-dynamic'`, which a prerendered page silently fails, and `img-src`
    and `connect-src` are derived from a build-time environment variable. Every
    one of those failure modes looks the same to the customer — a page that
    paints and does nothing — and none of them reaches a server log. This is how
    the first symptom stops being a phone call.

    Two body shapes, because browsers disagree. The old `report-uri` directive
    posts `application/csp-report` with one report under a `csp-report` key; the
    Reporting API posts `application/reports+json` with a list. Both are
    accepted rather than picking one, because which you get depends on the
    customer's browser and neither is going away soon.
    """

    throttle_scope = "reports"
    # `JSONParser` last is not cosmetic: some browsers post a violation as plain
    # `application/json`, and a hand-written curl during a deploy check
    # certainly will.
    parser_classes = [CspReportParser, ReportingApiParser, JSONParser]

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        for report in self._reports(request.data):
            logger.warning(
                "csp violation",
                extra={
                    # The directive and the blocked URI together are the whole
                    # diagnosis: "img-src blocked https://old-api.example" says
                    # exactly which environment variable is stale.
                    "directive": clean(
                        report.get("effective-directive")
                        or report.get("effectiveDirective")
                        or report.get("violated-directive"),
                        64,
                    ),
                    "blocked": clean(
                        report.get("blocked-uri") or report.get("blockedURL"), 500
                    ),
                    "document": clean(
                        report.get("document-uri") or report.get("documentURL"), 500
                    ),
                    "disposition": clean(report.get("disposition"), 16),
                },
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _reports(data) -> list[dict]:
        """The violations in this body, whichever shape the browser used."""
        if isinstance(data, dict):
            single = data.get("csp-report")
            return [single] if isinstance(single, dict) else []
        if isinstance(data, list):
            return [
                entry["body"]
                for entry in data
                if isinstance(entry, dict) and isinstance(entry.get("body"), dict)
            ]
        return []
