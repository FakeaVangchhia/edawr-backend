"""Views, one module per resource.

`app/routers/` became `api/views/`. The file split is the same one the FastAPI
version used, and for the same reason: it mirrors the resources, not the URLs.

Read them in this order if you are learning DRF:

    products.py    the full pattern — serializer in, serializer out, admin-only
    store.py       the smallest possible view, and why it is a separate file
    orders.py      mixed access in one module, and per-view permissions
    delivery.py    a composite response assembled in Python
    uploads.py     multipart instead of JSON
"""
