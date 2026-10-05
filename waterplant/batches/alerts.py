"""Proactive stock alerts: near expiry, expired and low stock."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .models import ACTIVE, EXPIRED, SPENT_STATUSES, Batch

NEAR_EXPIRY = "near_expiry"
EXPIRED_ALERT = "expired"
LOW_STOCK = "low_stock"


@dataclass(frozen=True)
class Alert:
    """One actionable inventory warning for the console."""

    kind: str
    chemical: str
    batch_id: str
    detail: str
    remaining: float
    days_to_expiry: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "chemical": self.chemical,
            "batch_id": self.batch_id,
            "detail": self.detail,
            "remaining": self.remaining,
            "days_to_expiry": self.days_to_expiry,
        }


def batch_alerts(batch: Batch, today: dt.date, near_expiry_days: int) -> list[Alert]:
    """Expiry alerts that apply to one batch.

    A batch that reaches its expiry date is reported as ``expired`` (not as
    near expiry); near expiry covers the inclusive window of the configured
    lead time so expiry day itself (day zero) is always flagged.
    """

    alerts: list[Alert] = []
    days = batch.days_to_expiry(today)
    if days is None:
        return alerts
    if days < 0:
        alerts.append(
            Alert(
                kind=EXPIRED_ALERT,
                chemical=batch.chemical,
                batch_id=batch.batch_id,
                detail=f"expired {-days} day(s) ago on {batch.expiry_date}",
                remaining=batch.remaining(),
                days_to_expiry=days,
            )
        )
    elif days <= near_expiry_days:
        alerts.append(
            Alert(
                kind=NEAR_EXPIRY,
                chemical=batch.chemical,
                batch_id=batch.batch_id,
                detail=f"expires in {days} day(s) on {batch.expiry_date}",
                remaining=batch.remaining(),
                days_to_expiry=days,
            )
        )
    return alerts


def collect_alerts(
    batches: list[Batch],
    today: dt.date,
    near_expiry_days: int,
    reorder_levels: dict[str, float],
    default_reorder_level: float,
) -> list[Alert]:
    """Expiry alerts per live batch plus a low stock row per chemical."""

    alerts: list[Alert] = []
    balances: dict[str, float] = {}
    for batch in batches:
        status = batch.derived_status(today)
        if status in SPENT_STATUSES:
            continue
        if status in (ACTIVE, EXPIRED) and batch.remaining() > 1e-9:
            balances[batch.chemical] = balances.get(batch.chemical, 0.0) + batch.remaining()
            alerts.extend(batch_alerts(batch, today, near_expiry_days))

    for chemical in sorted(balances):
        level = reorder_levels.get(chemical, default_reorder_level)
        if balances[chemical] < level:
            alerts.append(
                Alert(
                    kind=LOW_STOCK,
                    chemical=chemical,
                    batch_id="",
                    detail=f"balance {balances[chemical]:.4f} below reorder level {level:.4f}",
                    remaining=balances[chemical],
                )
            )
    return alerts
