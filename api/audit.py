"""Recording who did what.

One function, `record()`, called from the mutating admin and rider views. It is
deliberately tiny and deliberately best-effort.

**Why a helper and not a signal.** Django signals would catch every save
automatically, which sounds better until you need the *sentence*: "Cancelled
order #412 — customer not home" cannot be derived from a diff of two rows. The
useful half of an audit log is written at the call site, where the intent is
known, so the call site is where this is invoked.

**Why it never raises.** An audit failure must not fail the request that was
being audited. If the log table is missing or the JSON will not serialise, the
product edit that triggered it has already committed and refusing it now would
be worse than losing one row — so the exception is swallowed and logged. This is
the one place in this codebase where a bare `except` is the correct choice, and
it is why the call sites do not wrap it.
"""

from __future__ import annotations

import logging
from typing import Any

from api.models import AdminUser, AuditLog, User

logger = logging.getLogger(__name__)

# Never write these into `changes`, whatever the caller passes. A log that
# records password hashes is a second copy of the credential store.
REDACTED = frozenset({"password", "password_hash", "pin", "pin_hash", "access_token"})


def describe_actor(user: Any) -> dict[str, Any]:
    """Turn `request.user` into the four denormalised actor columns."""
    if isinstance(user, AdminUser):
        return {
            "actor_kind": AuditLog.ADMIN,
            "actor_id": user.pk,
            "actor_label": user.name or user.email,
            "actor_role": user.role,
        }
    if isinstance(user, User):
        return {
            "actor_kind": AuditLog.RIDER,
            "actor_id": user.pk,
            "actor_label": user.name,
            "actor_role": user.role,
        }
    return {
        "actor_kind": AuditLog.SYSTEM,
        "actor_id": None,
        "actor_label": "system",
        "actor_role": "",
    }


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[Any]]:
    """`{field: [before, after]}` for the fields that actually changed.

    Values are stringified because this lands in a JSONField and a Decimal is not
    JSON-serialisable — and because the log is read, not recomputed from.
    """
    changed: dict[str, list[Any]] = {}
    for key in set(before) | set(after):
        if key in REDACTED:
            continue
        old, new = before.get(key), after.get(key)
        if old != new:
            changed[key] = [None if old is None else str(old),
                            None if new is None else str(new)]
    return changed


def record(
    request,
    action: str,
    entity: str,
    entity_id: int | None = None,
    summary: str = "",
    changes: dict[str, Any] | None = None,
) -> None:
    """Write one audit row. Never raises; see the module docstring."""
    try:
        if changes:
            changes = {k: v for k, v in changes.items() if k not in REDACTED}
        AuditLog.objects.create(
            action=action,
            entity=entity,
            entity_id=entity_id,
            summary=summary[:255],
            changes=changes or None,
            **describe_actor(getattr(request, "user", None)),
        )
    except Exception:  # noqa: BLE001 — see the module docstring.
        logger.exception("audit write failed: %s %s#%s", action, entity, entity_id)
