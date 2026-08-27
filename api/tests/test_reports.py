"""The two endpoints the clients report their own failures to.

Both are public and unauthenticated, which is the whole reason they need
careful tests: a crash report is worth having precisely when nobody is signed
in, and a browser sends a CSP violation with no way to attach a token. Public
means anyone can post, so the allowlisting and the caps below are not tidiness —
they are what stops a log aggregator becoming a PII sink and a bill anybody can
run up.
"""

from __future__ import annotations

import json

from django.test import override_settings
from rest_framework.exceptions import Throttled

from api.tests.base import APITestBase


class ClientErrorTests(APITestBase):
    URL = "/api/client-errors"

    def setUp(self):
        super().setUp()
        self.as_anonymous()

    def post(self, body: dict):
        return self.client.post(self.URL, body, format="json")

    def test_a_report_is_accepted_without_a_token(self):
        with self.assertLogs("api.reports", level="WARNING") as logs:
            response = self.post({"client": "storefront", "message": "boom"})

        self.assertEqual(response.status_code, 204)
        self.assertIn("client error", logs.output[0])

    def test_the_fields_reach_the_log_record(self):
        with self.assertLogs("api.reports", level="WARNING") as logs:
            self.post(
                {
                    "client": "console",
                    "route": "/orders",
                    "message": "Cannot read properties of undefined",
                    "digest": "1234567890",
                    "stack": "at OrdersPage",
                }
            )

        record = logs.records[0]
        self.assertEqual(record.client, "console")
        self.assertEqual(record.route, "/orders")
        self.assertEqual(record.digest, "1234567890")
        # `error_message`, not `message` — the latter is reserved on a
        # LogRecord and setting it via `extra` raises a KeyError, which
        # would make every crash report a 500.
        self.assertIn("undefined", record.error_message)

    def test_unknown_keys_are_dropped(self):
        """The allowlist is the contract.

        Logging whatever arrives turns a public endpoint into storage a stranger
        controls: they can write unbounded volume, and they can write other
        people's personal data into the store's own logs, where it is now the
        store's problem under every privacy rule that applies.
        """
        with self.assertLogs("api.reports", level="WARNING") as logs:
            self.post(
                {
                    "message": "fine",
                    "password": "hunter2",
                    "customer_phone": "+919812345678",
                }
            )

        serialised = json.dumps(
            {k: str(v) for k, v in logs.records[0].__dict__.items()}
        )
        self.assertNotIn("hunter2", serialised)
        self.assertNotIn("+919812345678", serialised)

    def test_a_long_field_is_truncated(self):
        with self.assertLogs("api.reports", level="WARNING") as logs:
            self.post({"message": "x" * 50_000})

        self.assertLessEqual(len(logs.records[0].error_message), 2000)

    def test_a_non_string_field_does_not_crash_the_logger(self):
        """The body is JSON from an anonymous caller, so a field can be a number,
        a list or an object where a string was promised. An error path that can
        itself raise is an error path that gets retried in a loop."""
        with self.assertLogs("api.reports", level="WARNING") as logs:
            response = self.post(
                {"message": {"nested": True}, "route": 42, "stack": ["a", "b"]}
            )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(logs.records[0].route, "42")

    def test_an_empty_body_is_accepted(self):
        with self.assertLogs("api.reports", level="WARNING"):
            self.assertEqual(self.post({}).status_code, 204)

    def test_a_non_object_body_is_accepted(self):
        """A client crashing badly enough may send anything at all. 204 rather
        than 400: there is nothing useful to tell something that has just
        crashed, and a 400 invites a retry."""
        with self.assertLogs("api.reports", level="WARNING"):
            response = self.client.post(self.URL, ["not", "an", "object"], format="json")
        self.assertEqual(response.status_code, 204)

    def test_it_writes_nothing_to_the_database(self):
        from api.models import AuditLog, Order

        before = (AuditLog.objects.count(), Order.objects.count())
        with self.assertLogs("api.reports", level="WARNING"):
            self.post({"message": "boom"})

        self.assertEqual(
            (AuditLog.objects.count(), Order.objects.count()), before
        )


class CspReportTests(APITestBase):
    URL = "/api/csp-report"

    def setUp(self):
        super().setUp()
        self.as_anonymous()

    def test_the_legacy_report_uri_shape_is_accepted(self):
        """`application/csp-report` is what the `report-uri` directive posts.

        DRF matches parsers on the media type exactly, so without the parser in
        `views/reports.py` this is a 415 and the endpoint is decoration.
        """
        body = json.dumps(
            {
                "csp-report": {
                    "document-uri": "https://shop.example/cart",
                    "violated-directive": "img-src",
                    "effective-directive": "img-src",
                    "blocked-uri": "https://old-api.example/uploads/a.png",
                }
            }
        )

        with self.assertLogs("api.reports", level="WARNING") as logs:
            response = self.client.post(
                self.URL, body, content_type="application/csp-report"
            )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(logs.records[0].directive, "img-src")
        self.assertIn("old-api.example", logs.records[0].blocked)

    def test_the_reporting_api_shape_is_accepted(self):
        body = json.dumps(
            [
                {
                    "type": "csp-violation",
                    "body": {
                        "documentURL": "https://shop.example/",
                        "effectiveDirective": "script-src",
                        "blockedURL": "inline",
                        "disposition": "enforce",
                    },
                }
            ]
        )

        with self.assertLogs("api.reports", level="WARNING") as logs:
            response = self.client.post(
                self.URL, body, content_type="application/reports+json"
            )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(logs.records[0].directive, "script-src")
        self.assertEqual(logs.records[0].disposition, "enforce")

    def test_plain_json_is_accepted_too(self):
        """Some browsers post a violation as application/json, and anyone
        checking the endpoint with curl during a deploy certainly will."""
        with self.assertLogs("api.reports", level="WARNING"):
            response = self.client.post(
                self.URL,
                {"csp-report": {"effective-directive": "connect-src"}},
                format="json",
            )
        self.assertEqual(response.status_code, 204)

    def test_a_body_with_no_reports_logs_nothing_and_still_succeeds(self):
        response = self.client.post(self.URL, {}, format="json")
        self.assertEqual(response.status_code, 204)

    def test_a_batch_logs_one_line_per_violation(self):
        body = json.dumps(
            [
                {"body": {"effectiveDirective": "img-src"}},
                {"body": {"effectiveDirective": "font-src"}},
                {"not-a-report": True},
            ]
        )

        with self.assertLogs("api.reports", level="WARNING") as logs:
            self.client.post(self.URL, body, content_type="application/reports+json")

        self.assertEqual(len(logs.records), 2)


class ReportThrottleTests(APITestBase):
    """Public write endpoints need a ceiling, and a trip needs to be visible."""

    @override_settings()
    def test_reports_are_throttled(self):
        from rest_framework.throttling import SimpleRateThrottle

        original = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = {**original, "reports": "2/min"}
        try:
            self.as_anonymous()
            with self.assertLogs("api.reports", level="WARNING"):
                for _ in range(2):
                    self.assertEqual(
                        self.client.post(
                            "/api/client-errors", {"message": "x"}, format="json"
                        ).status_code,
                        204,
                    )

            response = self.client.post(
                "/api/client-errors", {"message": "x"}, format="json"
            )
            self.assertEqual(response.status_code, 429)
        finally:
            SimpleRateThrottle.THROTTLE_RATES = original


class ThrottleLoggingTests(APITestBase):
    """A rate limit that trips silently cannot be tuned or diagnosed."""

    def test_a_trip_writes_a_log_line(self):
        from rest_framework.throttling import SimpleRateThrottle

        original = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = {**original, "login": "1/min"}
        try:
            self.as_anonymous()
            credentials = {"email": "nobody@edawr.test", "password": "wrong"}
            self.client.post("/api/auth/login", credentials, format="json")

            with self.assertLogs("api.throttle", level="WARNING") as logs:
                response = self.client.post(
                    "/api/auth/login", credentials, format="json"
                )

            self.assertEqual(response.status_code, 429)
            record = logs.records[0]
            self.assertEqual(record.scope, "login")
            self.assertEqual(record.path, "/api/auth/login")
            self.assertFalse(record.authenticated)
            self.assertIsNotNone(record.retry_after)
        finally:
            SimpleRateThrottle.THROTTLE_RATES = original

    def test_the_client_is_still_told_how_long_to_wait(self):
        """The log must not have displaced the `Retry-After` header DRF sets."""
        from rest_framework.throttling import SimpleRateThrottle

        original = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = {**original, "login": "1/min"}
        try:
            self.as_anonymous()
            credentials = {"email": "nobody@edawr.test", "password": "wrong"}
            self.client.post("/api/auth/login", credentials, format="json")

            with self.assertLogs("api.throttle", level="WARNING"):
                response = self.client.post(
                    "/api/auth/login", credentials, format="json"
                )

            self.assertIn("Retry-After", response.headers)
        finally:
            SimpleRateThrottle.THROTTLE_RATES = original

    def test_an_ordinary_request_logs_nothing(self):
        self.as_anonymous()
        with self.assertNoLogs("api.throttle", level="WARNING"):
            self.client.get("/api/store/config")

    def test_the_error_shape_is_unchanged(self):
        """`Throttled` still has to come back as {"detail": ...} like everything
        else — the logging hook sits inside the handler that guarantees that."""
        self.assertTrue(issubclass(Throttled, Exception))

        from rest_framework.throttling import SimpleRateThrottle

        original = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = {**original, "login": "1/min"}
        try:
            self.as_anonymous()
            credentials = {"email": "nobody@edawr.test", "password": "wrong"}
            self.client.post("/api/auth/login", credentials, format="json")
            with self.assertLogs("api.throttle", level="WARNING"):
                response = self.client.post(
                    "/api/auth/login", credentials, format="json"
                )

            self.assertIsInstance(response.data.get("detail"), str)
        finally:
            SimpleRateThrottle.THROTTLE_RATES = original
