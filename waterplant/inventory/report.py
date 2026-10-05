"""Inventory projections, alerts and validation rules."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_EXPIRY_WARNING_DAYS = 30


@dataclass(frozen=True)
class InventoryState:
    """Batch ledger balances, fixed allocation policy and active warehouse alerts."""

    reorder_level: float
    balance: float
    available_balance: float
    needs_reorder: list[str]
    lots: list[dict[str, object]]
    alerts: list[dict[str, object]]
    strategy: str
    expiry_warning_days: int
    receipts: list[dict[str, object]]
    movements: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "reorder_level": self.reorder_level,
            "balance": self.balance,
            "available_balance": self.available_balance,
            "needs_reorder": self.needs_reorder,
            "lots": self.lots,
            "alerts": self.alerts,
            "allocation_strategy": self.strategy,
            "expiry_warning_days": self.expiry_warning_days,
            "receipts": self.receipts,
            "movements": self.movements,
        }


def validate_quantity(value: float, field_name: str = "amount") -> None:
    """Reject quantities outside the supported range."""

    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if value > 1_000_000:
        raise ValueError(f"{field_name} exceeds the supported range")


def validate_positive_quantity(value: float, field_name: str = "amount") -> None:
    validate_quantity(value, field_name)
    if value == 0:
        raise ValueError(f"{field_name} must be positive")


def validate_identifier(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} is required")


def validate_warning_days(value: int) -> None:
    if value < 0:
        raise ValueError("warning days must be non-negative")
    if value > 3650:
        raise ValueError("warning days exceeds the supported range")
