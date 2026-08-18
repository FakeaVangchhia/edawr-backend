"""Remove the demo catalogue and its sample orders, keeping every account.

`seed` writes 8 categories, 33 products and 5 orders so a fresh checkout has
something to browse. They are ordinary database rows — the storefront has no
hardcoded catalogue and never did — which means the way to stop showing them is
to delete the rows, and until now the only thing that deleted them was `seed`
itself, which also deletes every admin and every real order.

This is the half of `seed` you want on a store that is about to open: it clears
the sample data and leaves accounts, staff and anything you have added yourself
alone.

    uv run manage.py demo_clear --dry-run   # say what would go
    uv run manage.py demo_clear             # asks first
    uv run manage.py demo_clear --yes       # for scripts

**Deletion order is not arbitrary.** `OrderItem.product` is `on_delete=PROTECT`,
so a product referenced by any order refuses to be deleted and raises
`ProtectedError`. The demo orders therefore have to go before the demo products,
and a product referenced by a *real* order is kept and reported rather than
forced — deleting it would be rewriting a customer's receipt.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.management.commands.seed import CATEGORIES, PRODUCTS
from api.models import Category, Order, OrderItem, Product


def demo_product_names() -> set[str]:
    """The names `seed` writes, read from `seed` itself rather than copied.

    Deriving the list means the two commands cannot drift: a product added to the
    seed fixture is automatically something `demo_clear` knows how to remove.
    """
    names = set()
    for row in PRODUCTS:
        if isinstance(row, dict):
            name = row.get("name")
        else:
            name = row[0]
        if name:
            names.add(name)
    return names


def demo_category_names() -> set[str]:
    names = set()
    for row in CATEGORIES:
        if isinstance(row, dict):
            name = row.get("name")
        elif isinstance(row, str):
            name = row
        else:
            name = row[0]
        if name:
            names.add(name)
    return names


class Command(BaseCommand):
    help = "Delete seeded demo products, categories and sample orders. Keeps accounts."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Skip the confirmation.")
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would be deleted."
        )

    def handle(self, *args, **options):
        product_names = demo_product_names()
        category_names = demo_category_names()

        products = Product.objects.filter(name__in=product_names)
        categories = Category.objects.filter(name__in=category_names)

        # Only orders made up entirely of demo products. A real order that
        # happens to contain a seeded product is somebody's purchase and is not
        # sample data, whatever it is made of.
        demo_product_ids = set(products.values_list("id", flat=True))
        removable_orders = []
        for order in Order.objects.prefetch_related("items").all():
            item_products = {item.product_id for item in order.items.all()}
            if item_products and item_products <= demo_product_ids:
                removable_orders.append(order.pk)

        # Products still referenced by an order that is *not* being removed.
        # These are kept: PROTECT would refuse anyway, and rewriting a past
        # order's line items is not something a cleanup command should do.
        protected = set(
            OrderItem.objects.filter(product__in=products)
            .exclude(order_id__in=removable_orders)
            .values_list("product_id", flat=True)
        )
        deletable = products.exclude(pk__in=protected)

        self.stdout.write(f"Demo orders to delete:      {len(removable_orders)}")
        self.stdout.write(f"Demo products to delete:    {deletable.count()}")
        self.stdout.write(f"Demo products kept (in use):{len(protected):>4}")
        self.stdout.write(f"Demo categories to delete:  {categories.count()}")
        self.stdout.write("Accounts, staff and riders are not touched.")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run - nothing was deleted."))
            return

        if not options["yes"]:
            answer = input("Delete these rows? Type 'yes' to continue: ")
            if answer.strip().lower() != "yes":
                raise CommandError("Cancelled.")

        with transaction.atomic():
            # Children first: OrderItem.product is PROTECT, so the orders that
            # reference these products have to be gone before the products are.
            OrderItem.objects.filter(order_id__in=removable_orders).delete()
            Order.objects.filter(pk__in=removable_orders).delete()
            removed_products = deletable.count()
            deletable.delete()
            removed_categories = categories.count()
            categories.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Removed {len(removable_orders)} orders, {removed_products} products "
                f"and {removed_categories} categories."
            )
        )
        if protected:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(protected)} demo product(s) were kept because real orders "
                    'reference them. Set their status to "inactive" to hide them.'
                )
            )
