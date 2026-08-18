"""Every console action leaves a row saying who did it.

`Order` already carried five lifecycle timestamps, so the system could always say
*when* an order was packed and never by whom. With two roles that is no longer
good enough: "the manager cancelled it" and "the owner cancelled it" are
different facts, and a cash business needs to tell them apart afterwards.

Two properties are load-bearing and are asserted here rather than assumed:

- **Secrets never reach the log.** `audit.diff` redacts password and PIN fields,
  so a PIN reset records *that* it happened and never what to. A log that
  contains credentials is a second copy of the credential store.
- **An audit failure never fails the request.** `record()` swallows its own
  exceptions, because refusing a product edit that has already committed would
  be worse than losing one log row.
"""

from unittest import mock

from api.models import AuditLog, Order
from api.tests.base import APITestBase


class ProductAuditTests(APITestBase):
    def test_creating_a_product_is_recorded(self):
        admin = self.as_admin()
        response = self.client.post(
            "/api/products",
            {"name": "Audited Milk", "price": "50.00", "mrp": "60.00", "stock": 5},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        entry = AuditLog.objects.get(entity="product", action=AuditLog.CREATE)
        self.assertEqual(entry.entity_id, response.data["id"])
        self.assertEqual(entry.actor_kind, AuditLog.ADMIN)
        self.assertEqual(entry.actor_id, admin.pk)
        self.assertEqual(entry.actor_label, admin.email)
        self.assertIn("Audited Milk", entry.summary)

    def test_patch_records_only_what_changed(self):
        """The diff is the useful part. An entry listing eighteen unchanged
        fields buries the one that moved."""
        product = self.make_product(price="50.00", mrp="60.00")
        self.as_admin()

        response = self.client.patch(
            f"/api/products/{product.pk}", {"price": "45.00"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

        entry = AuditLog.objects.get(entity="product", action=AuditLog.UPDATE)
        self.assertEqual(set(entry.changes), {"price"})
        self.assertEqual(entry.changes["price"], ["50.00", "45.00"])

    def test_a_patch_that_changes_nothing_writes_nothing(self):
        product = self.make_product(price="50.00", mrp="60.00")
        self.as_admin()
        self.client.patch(
            f"/api/products/{product.pk}", {"price": "50.00"}, format="json"
        )
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.UPDATE).exists())

    def test_manager_actions_record_the_manager(self):
        """The point of the log: it distinguishes the two roles."""
        manager = self.as_manager()
        self.client.post(
            "/api/products",
            {"name": "Manager Milk", "price": "50.00", "mrp": "60.00", "stock": 5},
            format="json",
        )
        entry = AuditLog.objects.get(entity="product")
        self.assertEqual(entry.actor_id, manager.pk)
        self.assertEqual(entry.actor_role, "manager")


class OrderAuditTests(APITestBase):
    def test_status_change_records_the_transition(self):
        order = self.place_order(self.make_product(stock=50))
        self.as_admin()

        self.client.patch(
            f"/api/orders/{order.pk}/status", {"status": Order.PACKING}, format="json"
        )
        entry = AuditLog.objects.get(entity="order", action=AuditLog.STATUS)
        self.assertEqual(entry.entity_id, order.pk)
        self.assertEqual(entry.changes["status"], [Order.PLACED, Order.PACKING])

    def test_cancellation_records_the_reason(self):
        """The manager could not previously say *why* — the console hardcoded
        'Cancelled by store', which is the one thing nobody needs to be told."""
        order = self.place_order(self.make_product(stock=50))
        self.as_admin()

        response = self.client.patch(
            f"/api/orders/{order.pk}/status",
            {"status": Order.CANCELLED, "reason": "Nobody home after three calls"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        order.refresh_from_db()
        self.assertEqual(order.cancellation_reason, "Nobody home after three calls")
        entry = AuditLog.objects.get(entity="order", action=AuditLog.CANCEL)
        self.assertIn("Nobody home", entry.summary)

    def test_cancellation_without_a_reason_says_so(self):
        order = self.place_order(self.make_product(stock=50))
        self.as_admin()
        self.client.patch(
            f"/api/orders/{order.pk}/status", {"status": Order.CANCELLED}, format="json"
        )
        entry = AuditLog.objects.get(entity="order", action=AuditLog.CANCEL)
        self.assertIn("no reason given", entry.summary)

    def test_assignment_names_the_rider(self):
        rider = self.make_rider()
        order = self.place_order(self.make_product(stock=50))
        self.as_admin()

        response = self.client.post(
            f"/api/orders/{order.pk}/assign",
            {"delivery_boy_id": rider.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        entry = AuditLog.objects.get(entity="order", action=AuditLog.ASSIGN)
        self.assertIn(rider.name, entry.summary)

    def test_rider_transitions_are_attributed_to_the_rider(self):
        rider = self.make_rider()
        order = self.place_order(self.make_product(stock=50))
        self.as_admin()
        self.client.post(
            f"/api/orders/{order.pk}/assign", {"delivery_boy_id": rider.pk},
            format="json",
        )

        self.as_rider(rider)
        self.client.patch(
            f"/api/orders/{order.pk}/status", {"status": Order.DELIVERED},
            format="json",
        )

        entry = AuditLog.objects.filter(action=AuditLog.STATUS).latest("id")
        self.assertEqual(entry.actor_kind, AuditLog.RIDER)
        self.assertEqual(entry.actor_id, rider.pk)


class SecretRedactionTests(APITestBase):
    def test_a_pin_reset_is_recorded_but_the_pin_is_not(self):
        rider = self.make_rider()
        self.as_admin()

        response = self.client.put(
            f"/api/users/{rider.pk}", {"pin": "7391"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

        entry = AuditLog.objects.get(entity="staff", action=AuditLog.UPDATE)
        # The marker is deliberately not called "pin": `record()` strips keys
        # named like credentials, so the marker would be stripped with them.
        self.assertEqual(entry.changes["pin_reset"], ["no", "yes"])
        self.assertNotIn("pin", entry.changes)
        self.assertNotIn("pin_hash", entry.changes)
        self.assertNotIn("7391", str(entry.changes))

    def test_an_account_password_never_reaches_the_log(self):
        self.as_admin()
        victim = self.make_manager()
        self.client.put(
            f"/api/admins/{victim.pk}", {"password": "a-brand-new-password"},
            format="json",
        )
        entry = AuditLog.objects.get(entity="admin", action=AuditLog.UPDATE)
        self.assertEqual(entry.changes["password_reset"], ["no", "yes"])
        self.assertNotIn("password", entry.changes)
        self.assertNotIn("a-brand-new-password", str(entry.changes))


class ResilienceTests(APITestBase):
    def test_a_failing_audit_write_does_not_fail_the_request(self):
        """The product edit has already committed. Refusing it now because the
        log could not be written would be a worse outcome than losing one row."""
        self.as_admin()
        with mock.patch.object(
            AuditLog.objects, "create", side_effect=RuntimeError("log is down")
        ):
            response = self.client.post(
                "/api/products",
                {"name": "Survives", "price": "50.00", "mrp": "60.00", "stock": 5},
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.data)


class AuditReadTests(APITestBase):
    def test_only_an_admin_may_read_the_log(self):
        self.as_manager()
        self.assertEqual(self.client.get("/api/audit").status_code, 403)

    def test_filters_narrow_the_log(self):
        self.as_admin()
        self.client.post(
            "/api/products",
            {"name": "One", "price": "10.00", "mrp": "10.00", "stock": 1},
            format="json",
        )
        self.client.post(
            "/api/categories", {"name": "A Category"}, format="json"
        )

        everything = self.client.get("/api/audit")
        self.assertEqual(len(everything.data), 2)
        self.assertEqual(everything["X-Total-Count"], "2")

        products = self.client.get("/api/audit?entity=product")
        self.assertEqual(len(products.data), 1)
        self.assertEqual(products.data[0]["entity"], "product")

    def test_newest_first(self):
        self.as_admin()
        for name in ("First", "Second"):
            self.client.post(
                "/api/products",
                {"name": name, "price": "10.00", "mrp": "10.00", "stock": 1},
                format="json",
            )
        data = self.client.get("/api/audit").data
        self.assertIn("Second", data[0]["summary"])
