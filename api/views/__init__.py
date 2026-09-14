"""Views, one module per resource. The split mirrors the resources, not the URLs.

Where to start:

    products.py    the full pattern — serializer in, serializer out, admin-only
    store.py       everything a customer without an account can reach, and why
                   the module boundary is a security control
    orders.py      mixed access in one module, and per-view permissions
    delivery.py    the rider app: a composite response assembled in Python
    customer.py    a signed-in customer's own data, taken from the token
    auth.py        the three logins, and why 401 and 403 mean different things
    analytics.py   every figure the console shows, aggregated in the database
    uploads.py     multipart instead of JSON, and what a file *is*
"""
