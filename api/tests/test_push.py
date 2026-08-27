"""Waking a rider's phone: registration, dispatch hooks and the Expo send.

Three separable things, tested separately:

**Registration** — `POST/DELETE /api/delivery/push-token`. A rider identifies
themselves with a bearer token and a handset with an Expo token, and the second
is claimed rather than accumulated.

**The hooks** — assigning an order notifies the assignee; failing to assign one
notifies everybody who could take it. These assert *who* gets told, not what
crosses the network.

**The send** — `api/push._send`, driven directly with a stubbed Expo. It is the
one part that talks to a third party, so nothing here does; `urlopen` is patched
throughout, and a test that reaches the real gateway would be a test that fails
when Expo has an outage.

`PUSH_ENABLED` is off by default (see config/settings.py), so every test that
wants a notification turns it on explicitly. That is the point of the default:
the suite proves both halves of the switch.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.test import override_settings

from api import push
from api.models import Order, RiderDevice
from api.tests.base import APITestBase

TOKEN_A = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
TOKEN_B = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"

# ~4 km north of the store, and ~40 km away — the same two points test_dispatch
# uses, so "in range" and "out of range" mean the same thing in both files.
NEAR = (23.7640, 92.7178)
FAR = (24.0900, 92.7178)


class FakeResponse:
    """The two methods `urllib.request.urlopen` is used through here."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok_tickets(count: int) -> FakeResponse:
    return FakeResponse({"data": [{"status": "ok", "id": f"t{i}"} for i in range(count)]})


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
class DeviceRegistrationTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.rider = self.make_rider()

    def test_rider_registers_a_handset(self):
        self.as_rider(self.rider)
        response = self.client.post(
            "/api/delivery/push-token",
            {"expo_token": TOKEN_A, "platform": "android"},
            format="json",
        )

        self.assertEqual(response.status_code, 204, response.data)
        device = RiderDevice.objects.get(expo_token=TOKEN_A)
        self.assertEqual(device.rider_id, self.rider.id)
        self.assertEqual(device.platform, "android")

    def test_registering_twice_keeps_one_row(self):
        """The app re-registers on every launch; that must not grow the table."""
        self.as_rider(self.rider)
        for _ in range(3):
            self.client.post(
                "/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json"
            )

        self.assertEqual(RiderDevice.objects.filter(expo_token=TOKEN_A).count(), 1)

    def test_a_handset_belongs_to_whoever_signed_in_last(self):
        """Shift change on a shared phone must not buzz two riders for one drop.

        The unique constraint on `expo_token` is what makes this true — without
        it the phone would hold a row per rider who ever used it, and every
        order assigned to any of them would arrive on it.
        """
        other = self.make_rider(name="Rider Two", phone="+919000000003")

        self.as_rider(self.rider)
        self.client.post("/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json")
        self.as_rider(other)
        self.client.post("/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json")

        devices = RiderDevice.objects.filter(expo_token=TOKEN_A)
        self.assertEqual(devices.count(), 1)
        self.assertEqual(devices.first().rider_id, other.id)

    def test_a_rider_may_hold_several_handsets(self):
        self.as_rider(self.rider)
        self.client.post("/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json")
        self.client.post("/api/delivery/push-token", {"expo_token": TOKEN_B}, format="json")

        self.assertEqual(RiderDevice.objects.filter(rider=self.rider).count(), 2)

    def test_a_raw_device_token_is_refused(self):
        """400 while the rider is holding the phone beats a silent no-op later."""
        self.as_rider(self.rider)
        response = self.client.post(
            "/api/delivery/push-token",
            {"expo_token": "fj4Kd9s-not-an-expo-token"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(RiderDevice.objects.exists())

    def test_sign_out_forgets_the_handset(self):
        self.as_rider(self.rider)
        self.client.post("/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json")

        response = self.client.delete(
            "/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json"
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(RiderDevice.objects.exists())

    def test_forgetting_an_unknown_handset_is_still_204(self):
        """Sign-out cannot know whether the row is there, and wants the same end."""
        self.as_rider(self.rider)
        response = self.client.delete(
            "/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json"
        )
        self.assertEqual(response.status_code, 204)

    def test_a_rider_cannot_forget_someone_elses_handset(self):
        other = self.make_rider(name="Rider Two", phone="+919000000003")
        RiderDevice.objects.create(rider=other, expo_token=TOKEN_A)

        self.as_rider(self.rider)
        response = self.client.delete(
            "/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json"
        )

        # Idempotent from the caller's side, but the row is not theirs to remove.
        self.assertEqual(response.status_code, 204)
        self.assertTrue(RiderDevice.objects.filter(expo_token=TOKEN_A).exists())

    def test_registration_needs_a_rider_token(self):
        self.as_anonymous()
        response = self.client.post(
            "/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_an_admin_token_is_the_wrong_kind(self):
        """403, not 401 — we know exactly who this is, and it is not a rider."""
        self.as_admin()
        response = self.client.post(
            "/api/delivery/push-token", {"expo_token": TOKEN_A}, format="json"
        )
        self.assertEqual(response.status_code, 403)


# --------------------------------------------------------------------------
# The dispatch hooks
# --------------------------------------------------------------------------
@override_settings(PUSH_ENABLED=True)
class NotificationOnDispatchTests(APITestBase):
    """Who gets told, driven through the real HTTP status endpoint.

    `_send_in_background` is patched rather than `urlopen`, so nothing here
    starts a thread — the assertion is about the batch that was handed over,
    which is the last point the outcome is still deterministic.

    `captureOnCommitCallbacks` is not optional: `api/push.py` defers every send
    to `transaction.on_commit`, and `APITestBase` rolls each test back rather
    than committing, so without it the callbacks never run and every one of
    these would pass by asserting on nothing.
    """

    def setUp(self):
        super().setUp()
        self.product = self.make_product(stock=50)

    def ready(self, order: Order) -> None:
        self.as_admin()
        self.client.patch(
            f"/api/orders/{order.id}/status", {"status": Order.PACKING}, format="json"
        )
        self.client.patch(
            f"/api/orders/{order.id}/status", {"status": Order.READY}, format="json"
        )

    @staticmethod
    def recipients(sent) -> set[str]:
        """Every token across every batch handed to the sender."""
        return {message["to"] for call in sent.call_args_list for message in call.args[0]}

    def test_the_assigned_rider_is_notified(self):
        rider = self.make_rider()
        RiderDevice.objects.create(rider=rider, expo_token=TOKEN_A)
        order = self.place_order(self.product)

        with patch("api.push._send_in_background") as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.ready(order)

        self.assertEqual(self.recipients(sent), {TOKEN_A})
        batch = sent.call_args.args[0]
        self.assertEqual(batch[0]["title"], "New delivery assigned")
        self.assertEqual(batch[0]["data"]["type"], "assigned")
        self.assertEqual(batch[0]["data"]["order_id"], order.id)

    def test_only_the_assigned_rider_is_notified(self):
        """The near rider gets the order, so the far one must not be buzzed."""
        near = self.make_rider(name="Near", phone="+919000000101", latitude=NEAR[0], longitude=NEAR[1])
        far = self.make_rider(
            name="Far", phone="+919000000102",
            latitude=FAR[0], longitude=FAR[1], radius=100.0,
        )
        RiderDevice.objects.create(rider=near, expo_token=TOKEN_A)
        RiderDevice.objects.create(rider=far, expo_token=TOKEN_B)

        with patch("api.push._send_in_background") as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.ready(self.place_order(self.product))

        self.assertEqual(self.recipients(sent), {TOKEN_A})

    def test_the_address_is_in_the_body_and_the_phone_number_is_not(self):
        """A notification renders on a lock screen; it carries the minimum."""
        rider = self.make_rider()
        RiderDevice.objects.create(rider=rider, expo_token=TOKEN_A)
        order = self.place_order(self.product)

        with patch("api.push._send_in_background") as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.ready(order)

        body = sent.call_args.args[0][0]["body"]
        self.assertIn(order.customer_address.splitlines()[0], body)
        self.assertNotIn(order.customer_phone, body)
        self.assertNotIn(order.customer_name, body)

    @override_settings(AUTO_ASSIGN_RIDER=False)
    def test_the_pool_is_notified_when_nothing_is_assigned(self):
        """Automatic assignment off means the feed is dispatch; tell the feed."""
        one = self.make_rider(name="One", phone="+919000000101")
        two = self.make_rider(name="Two", phone="+919000000102")
        RiderDevice.objects.create(rider=one, expo_token=TOKEN_A)
        RiderDevice.objects.create(rider=two, expo_token=TOKEN_B)

        with patch("api.push._send_in_background") as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.ready(self.place_order(self.product))

        self.assertEqual(self.recipients(sent), {TOKEN_A, TOKEN_B})
        self.assertEqual(sent.call_args.args[0][0]["title"], "Order ready for pickup")
        self.assertEqual(sent.call_args.args[0][0]["data"]["type"], "pool")

    @override_settings(AUTO_ASSIGN_RIDER=False)
    def test_a_rider_out_of_range_is_not_notified(self):
        near = self.make_rider(name="Near", phone="+919000000101", latitude=NEAR[0], longitude=NEAR[1])
        far = self.make_rider(name="Far", phone="+919000000102", latitude=FAR[0], longitude=FAR[1])
        RiderDevice.objects.create(rider=near, expo_token=TOKEN_A)
        RiderDevice.objects.create(rider=far, expo_token=TOKEN_B)

        with patch("api.push._send_in_background") as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.ready(self.place_order(self.product))

        self.assertEqual(self.recipients(sent), {TOKEN_A})

    def test_a_manager_assigning_by_hand_notifies_the_rider(self):
        """The override path: an off-shift rider a manager has just spoken to.

        `other` is unavailable, so automatic assignment would never pick them and
        the notification can only have come from the assign endpoint.
        """
        other = self.make_rider(name="Two", phone="+919000000102", available=False)
        RiderDevice.objects.create(rider=other, expo_token=TOKEN_B)
        order = self.place_order(self.product)

        self.as_admin()
        with patch("api.push._send_in_background") as sent:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    f"/api/orders/{order.id}/assign",
                    {"delivery_boy_id": other.id},
                    format="json",
                )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.recipients(sent), {TOKEN_B})

    def test_a_rider_with_no_registered_handset_costs_nothing(self):
        """No devices, no send at all — not an empty batch to Expo."""
        self.make_rider()

        with patch("api.push._send_in_background") as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.ready(self.place_order(self.product))

        sent.assert_not_called()

    @override_settings(PUSH_ENABLED=False)
    def test_the_switch_turns_it_off_completely(self):
        rider = self.make_rider()
        RiderDevice.objects.create(rider=rider, expo_token=TOKEN_A)

        with patch("api.push._send_in_background") as sent:
            with self.captureOnCommitCallbacks(execute=True):
                self.ready(self.place_order(self.product))

        sent.assert_not_called()

    def test_the_order_is_still_dispatched_when_the_send_blows_up(self):
        """The whole point of the best-effort contract, asserted on the order.

        `notify_assigned` is called inside the transaction that assigns the
        order. If it could raise, a failure in someone else's push service
        would roll back a dispatch that has nothing to do with it.
        """
        rider = self.make_rider()
        RiderDevice.objects.create(rider=rider, expo_token=TOKEN_A)
        order = self.place_order(self.product)

        with patch("api.push._send_in_background", side_effect=RuntimeError("boom")):
            with self.captureOnCommitCallbacks(execute=True):
                self.ready(order)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.DISPATCHED)
        self.assertEqual(order.delivery_boy_id, rider.id)


# --------------------------------------------------------------------------
# The send itself
# --------------------------------------------------------------------------
@override_settings(PUSH_ENABLED=True, EXPO_ACCESS_TOKEN="")
class ExpoSendTests(APITestBase):
    """`push._send` against a stubbed gateway. Nothing here touches the network."""

    def setUp(self):
        super().setUp()
        self.rider = self.make_rider()

    def messages(self, *tokens) -> list[dict]:
        return [push._message(token, "Title", "Body", {"type": "test"}) for token in tokens]

    def test_it_posts_the_batch_to_the_configured_url(self):
        with patch("urllib.request.urlopen", return_value=ok_tickets(1)) as urlopen:
            push._send(self.messages(TOKEN_A))

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://exp.host/--/api/v2/push/send")
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body[0]["to"], TOKEN_A)
        self.assertEqual(body[0]["channelId"], push.CHANNEL_ID)
        self.assertEqual(body[0]["priority"], "high")

    def test_no_access_token_means_no_authorization_header(self):
        """An empty bearer is worse than none — Expo rejects the whole request."""
        with patch("urllib.request.urlopen", return_value=ok_tickets(1)) as urlopen:
            push._send(self.messages(TOKEN_A))

        headers = urlopen.call_args.args[0].headers
        self.assertNotIn("Authorization", headers)

    @override_settings(EXPO_ACCESS_TOKEN="secret-value")
    def test_an_access_token_is_sent_as_a_bearer(self):
        with patch("urllib.request.urlopen", return_value=ok_tickets(1)) as urlopen:
            push._send(self.messages(TOKEN_A))

        headers = urlopen.call_args.args[0].headers
        self.assertEqual(headers["Authorization"], "Bearer secret-value")

    def test_a_dead_token_is_deleted(self):
        """`DeviceNotRegistered` is the app being gone; stop keeping the row."""
        RiderDevice.objects.create(rider=self.rider, expo_token=TOKEN_A)
        RiderDevice.objects.create(rider=self.rider, expo_token=TOKEN_B)
        response = FakeResponse(
            {
                "data": [
                    {"status": "error", "details": {"error": "DeviceNotRegistered"}},
                    {"status": "ok", "id": "t1"},
                ]
            }
        )

        with patch("urllib.request.urlopen", return_value=response):
            push._send(self.messages(TOKEN_A, TOKEN_B))

        self.assertFalse(RiderDevice.objects.filter(expo_token=TOKEN_A).exists())
        self.assertTrue(RiderDevice.objects.filter(expo_token=TOKEN_B).exists())

    def test_any_other_error_keeps_the_token_and_is_logged(self):
        """A transient failure is not a reason to stop notifying a working phone.

        The log assertion is not incidental. Expo puts its explanation in a field
        called `message`, and passing that straight through as a logging `extra`
        raises `KeyError: Attempt to overwrite 'message' in LogRecord` — losing
        the one record that a notification failed, inside the `except` that keeps
        this module best-effort. It would never have shown up as a test failure.
        """
        RiderDevice.objects.create(rider=self.rider, expo_token=TOKEN_A)
        response = FakeResponse(
            {
                "data": [
                    {
                        "status": "error",
                        "message": "Rate limit exceeded",
                        "details": {"error": "MessageRateExceeded"},
                    }
                ]
            }
        )

        with patch("urllib.request.urlopen", return_value=response):
            with self.assertLogs("api.push", level="WARNING") as logs:
                push._send(self.messages(TOKEN_A))

        self.assertTrue(RiderDevice.objects.filter(expo_token=TOKEN_A).exists())
        self.assertIn("could not deliver", logs.output[0])

    def test_tickets_map_back_positionally(self):
        """One message per token is what makes this mapping possible at all."""
        RiderDevice.objects.create(rider=self.rider, expo_token=TOKEN_A)
        RiderDevice.objects.create(rider=self.rider, expo_token=TOKEN_B)
        response = FakeResponse(
            {
                "data": [
                    {"status": "ok", "id": "t0"},
                    {"status": "error", "details": {"error": "DeviceNotRegistered"}},
                ]
            }
        )

        with patch("urllib.request.urlopen", return_value=response):
            push._send(self.messages(TOKEN_A, TOKEN_B))

        self.assertTrue(RiderDevice.objects.filter(expo_token=TOKEN_A).exists())
        self.assertFalse(RiderDevice.objects.filter(expo_token=TOKEN_B).exists())

    def test_a_network_failure_is_swallowed(self):
        RiderDevice.objects.create(rider=self.rider, expo_token=TOKEN_A)

        with patch("urllib.request.urlopen", side_effect=OSError("no route to host")):
            push._send(self.messages(TOKEN_A))  # must not raise

        self.assertTrue(RiderDevice.objects.filter(expo_token=TOKEN_A).exists())

    def test_a_request_level_rejection_deletes_nothing(self):
        """`{"errors": [...]}` is our bug, not the handset's."""
        RiderDevice.objects.create(rider=self.rider, expo_token=TOKEN_A)
        response = FakeResponse({"errors": [{"code": "VALIDATION_ERROR"}]})

        with patch("urllib.request.urlopen", return_value=response):
            push._send(self.messages(TOKEN_A))

        self.assertTrue(RiderDevice.objects.filter(expo_token=TOKEN_A).exists())

    def test_batches_are_split_at_the_expo_limit(self):
        messages = self.messages(*[f"ExponentPushToken[{i:022d}]" for i in range(150)])

        with patch("urllib.request.urlopen", return_value=ok_tickets(100)) as urlopen:
            push._send(messages)

        self.assertEqual(urlopen.call_count, 2)
        first = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
        second = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))
        self.assertEqual(len(first), push.MAX_BATCH)
        self.assertEqual(len(second), 50)
