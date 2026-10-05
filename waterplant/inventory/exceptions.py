"""Inventory errors that map to specific console responses."""

from __future__ import annotations


class InventoryConflict(ValueError):
    """An idempotency key was reused with a different request."""

    status = 409


class InsufficientInventory(ValueError):
    """Requested stock cannot be supplied from usable, unexpired lots."""

    status = 400
