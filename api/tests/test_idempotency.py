"""Checkout survives being sent twice.

The failure this prevents is not exotic. A customer in Aizawl checks out on
mobile data, the request reaches the server and commits, the response is lost on
the way back, and the browser — or the customer, or a service worker — sends it
again. Without a key the second request is indistinguishable from a second
order: two orders, two cash amounts owed, and the shelf decremented twice for
goods that will be picked once.

The tests are grouped by which of the three paths in `checkout.place_order`
they exercise: the sequential retry (a plain lookup), the first attempt, and the
concurrent retry (the unique constraint).
"""

from __future__ import annotations

import threading
import uuid

from django.db import connections

from api.checkout import BasketUnavailable, place_order
from api.models import Order
from api.tests.base import APITestBase, APITransactionTestBase


class IdempotentCheckoutTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.product = self.make_product(price="60.00", stock=10)
        self.key = str(uuid.uuid4())

    def checkout(self, key: str | None = None, quantity: int = 2, **overrides):
        self.as_anonymous()
        headers = {}
        if key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        return self.client.post(
            "/api/store/orders",
            self.checkout_payload(self.product, quantity, **overrides),
            format="json",
            **headers,
        )

    # --- the sequential retry -------------------------------------------
    def test_replaying_a_key_returns_the_same_order(self):
        first = self.checkout(self.key)
        second = self.checkout(self.key)

        self.assertEqual(first.status_code, 201)
        # 200, not 201: the second request created nothing, and telling a client
        # it did is a lie it could act on.
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(
            first.data["tracking_token"], second.data["tracking_token"]
        )
        self.assertEqual(Order.objects.count(), 1)

    def test_a_replay_does_not_move_stock_a_second_time(self):
        """The whole point. Two charges is bad; two decrements is worse, because
        the shelf and the database disagree and nobody finds out until a picker
        cannot find the goods."""
        self.checkout(self.key, quantity=3)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 7)

        self.checkout(self.key, quantity=3)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 7)

    def test_a_replay_wins_over_a_changed_basket(self):
        """A key identifies an *attempt*, not a basket.

        If the customer's first request succeeded and their retry carries a
        different basket, the retry is still the same attempt — the difference
        means the client rebuilt the body, not that a second order was wanted.
        Honouring the new basket would create the duplicate the key exists to
        prevent, so the original order wins and the response says 200.
        """
        first = self.checkout(self.key, quantity=2)

        other = self.make_product(name="Something else", price="99.00", stock=10)
        self.as_anonymous()
        second = self.client.post(
            "/api/store/orders",
            self.checkout_payload(other, 5),
            format="json",
            HTTP_IDEMPOTENCY_KEY=self.key,
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(Order.objects.count(), 1)
        other.refresh_from_db()
        self.assertEqual(other.stock, 10)  # untouched

    # --- no key ----------------------------------------------------------
    def test_without_a_key_nothing_changes(self):
        """Backwards compatibility is the reason the column is nullable."""
        first = self.checkout()
        second = self.checkout()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.data["id"], second.data["id"])
        self.assertEqual(Order.objects.count(), 2)

    def test_many_key_less_orders_coexist(self):
        """The unique index must not collapse them.

        This is the trap `Order.idempotency_key` sidesteps by storing NULL
        rather than "": Postgres treats NULLs as distinct in a unique index but
        two empty strings as a duplicate, so a literal "" would let exactly one
        key-less order ever exist and 500 on the second.
        """
        for _ in range(3):
            self.assertEqual(self.checkout(quantity=1).status_code, 201)

        self.assertEqual(Order.objects.count(), 3)
        self.assertEqual(Order.objects.filter(idempotency_key__isnull=True).count(), 3)

    def test_an_empty_header_is_treated_as_no_key(self):
        first = self.checkout("")
        second = self.checkout("   ")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(Order.objects.count(), 2)

    # --- bad input -------------------------------------------------------
    def test_an_over_long_key_is_refused_rather_than_truncated(self):
        """Truncating would make two distinct keys collide silently, and the
        symptom of that is one customer being handed another's order."""
        response = self.checkout("k" * 65)

        self.assertEqual(response.status_code, 400)
        self.assertIn("64", response.data["detail"])
        self.assertEqual(Order.objects.count(), 0)

    def test_a_key_is_not_reusable_across_a_failed_attempt(self):
        """A checkout that raised wrote nothing, key included, so the customer's
        retry is a first attempt rather than a replay of a failure."""
        self.product.stock = 0
        self.product.save(update_fields=["stock"])
        self.assertEqual(self.checkout(self.key).status_code, 409)
        self.assertEqual(Order.objects.count(), 0)

        self.product.stock = 10
        self.product.save(update_fields=["stock"])
        self.assertEqual(self.checkout(self.key).status_code, 201)
        self.assertEqual(Order.objects.count(), 1)


class ConcurrentCheckoutTests(APITransactionTestBase):
    """Two real database connections, racing.

    On `APITransactionTestBase` rather than `APITestBase`, and that is the whole
    reason the split exists: the ordinary base wraps each test in a transaction
    it rolls back, so a second thread — which opens its own connection — cannot
    see anything the first one wrote, and a concurrency test written against it
    passes while testing nothing.

    Both tests here exercise invariants `deployment.md` lists as verified only
    by argument.
    """

    def setUp(self):
        super().setUp()
        # Stock to spare by default. The last-unit test sets it to 1 itself —
        # if it were 1 here, the key-race test below would never reach the
        # unique constraint, because the loser would be turned away by the stock
        # check first and the test would pass without testing anything.
        self.product = self.make_product(price="60.00", stock=10)

    @staticmethod
    def _run_together(target, count: int = 2) -> list:
        """Run `target` on `count` threads, released together, and collect results."""
        start = threading.Barrier(count)
        results: list = []
        lock = threading.Lock()

        def wrapped(index: int):
            try:
                start.wait(timeout=5)
                outcome = target(index)
            except Exception as exc:  # noqa: BLE001 — the test asserts on it
                outcome = exc
            finally:
                # Each thread opens its own connection; leaving them open leaks
                # into the next test and eventually exhausts the pool.
                connections.close_all()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        return results

    def test_the_same_key_on_two_connections_yields_one_order(self):
        """The concurrent retry: neither request can see the other's uncommitted
        row, so the unique constraint is the only thing standing between them
        and two orders."""
        key = str(uuid.uuid4())
        payload = self.checkout_payload(self.product, 1)

        def attempt(_index):
            order, created = place_order(payload, key)
            return (order.pk, created)

        results = self._run_together(attempt)

        self.assertEqual(len(results), 2, results)
        for result in results:
            self.assertNotIsInstance(result, Exception, result)

        ids = {pk for pk, _ in results}
        self.assertEqual(len(ids), 1, "both callers must get the same order")
        # Exactly one of them created it; the other was handed the winner.
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(Order.objects.filter(idempotency_key=key).count(), 1)

        # And the loser's rollback took its stock decrement with it. This is the
        # assertion that would catch a future refactor moving the INSERT outside
        # the transaction: one order placed, one unit gone, not two.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 9)

    def test_the_last_unit_cannot_be_sold_twice(self):
        """`select_for_update()` in `read_basket`, finally tested rather than
        argued for.

        This is the invariant `api/checkout.py`'s module docstring is mostly
        about, and it was never verified because the suite used to run on
        SQLite, where the lock is a no-op. Both local development and CI are on
        Postgres now, so the lock is real and so is this test.
        """
        self.product.stock = 1
        self.product.save(update_fields=["stock"])

        def attempt(index):
            payload = self.checkout_payload(self.product, 1)
            payload["customer_name"] = f"Customer {index}"
            try:
                order, _ = place_order(payload)
                return order.pk
            except BasketUnavailable:
                return "unavailable"

        results = self._run_together(attempt)

        self.assertEqual(len(results), 2, results)
        for result in results:
            self.assertNotIsInstance(result, Exception, result)

        # One sale, one honest refusal — never two sales of one packet.
        self.assertEqual(results.count("unavailable"), 1, results)
        self.assertEqual(Order.objects.count(), 1)

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 0)
