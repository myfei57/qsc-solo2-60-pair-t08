"""Component health checks."""

from __future__ import annotations

from .runtime import Runtime


def run_checks(rt: Runtime) -> list[dict[str, str]]:
    """Return one check row per mutable component."""

    checks = [{"name": "store", "status": "ok", "detail": f"keys={rt.store.count()}"}]
    if rt.calibration.current() <= 0:
        checks.append({"name": "calibration", "status": "fail", "detail": "factor must be positive"})
    else:
        checks.append({"name": "calibration", "status": "ok", "detail": "factor set"})
    if rt.well.residual_target() <= 0:
        checks.append({"name": "residual", "status": "fail", "detail": "target must be positive"})
    else:
        checks.append({"name": "residual", "status": "ok", "detail": "target set"})
    if rt.accumulator.value() < 0:
        checks.append({"name": "quota", "status": "fail", "detail": "accumulator is negative"})
    else:
        checks.append({"name": "quota", "status": "ok", "detail": "accumulator set"})
    if rt.flow_repository.load_flow()[1]:
        checks.append({"name": "intake", "status": "ok", "detail": "flow reading present"})
    else:
        checks.append({"name": "intake", "status": "warn", "detail": "no flow reading yet"})
    if rt.stabilizer.is_stable():
        checks.append({"name": "ph", "status": "ok", "detail": "ph in the stable band"})
    else:
        checks.append({"name": "ph", "status": "warn", "detail": "ph outside the stable band"})
    alerts = rt.inventory.needs_reorder()
    if alerts:
        checks.append(
            {"name": "inventory", "status": "warn", "detail": "reorder " + ", ".join(alerts)}
        )
    else:
        checks.append({"name": "inventory", "status": "ok", "detail": "stock above reorder level"})

    batch_state = rt.batch_service.state()
    batch_alerts = batch_state["alerts"]
    expired = [row for row in batch_alerts if row["kind"] == "expired"]
    near = [row for row in batch_alerts if row["kind"] == "near_expiry"]
    low = [row for row in batch_alerts if row["kind"] == "low_stock"]
    if expired:
        checks.append(
            {
                "name": "batches-expired",
                "status": "fail",
                "detail": "expired batches: " + ", ".join(row["batch_id"] for row in expired),
            }
        )
    else:
        checks.append({"name": "batches-expired", "status": "ok", "detail": "no expired stock"})
    if near:
        checks.append(
            {
                "name": "batches-near-expiry",
                "status": "warn",
                "detail": "expiring soon: " + ", ".join(row["batch_id"] for row in near),
            }
        )
    else:
        checks.append(
            {"name": "batches-near-expiry", "status": "ok", "detail": "nothing near expiry"}
        )
    if low:
        checks.append(
            {
                "name": "batches-low-stock",
                "status": "warn",
                "detail": "low stock: " + ", ".join(row["chemical"] for row in low),
            }
        )
    else:
        checks.append({"name": "batches-low-stock", "status": "ok", "detail": "stock levels healthy"})
    pending_quarantine = batch_state["quarantine_pending"]
    pending_inbound = batch_state["inbound_pending"]
    if pending_quarantine:
        checks.append(
            {
                "name": "batches-quarantine",
                "status": "warn",
                "detail": f"{pending_quarantine} conflicting resend(s) awaiting manual review",
            }
        )
    else:
        checks.append({"name": "batches-quarantine", "status": "ok", "detail": "no quarantined resends"})
    if pending_inbound:
        checks.append(
            {
                "name": "batches-inbound",
                "status": "warn",
                "detail": f"{pending_inbound} interrupted inbound order(s) need recovery",
            }
        )
    else:
        checks.append({"name": "batches-inbound", "status": "ok", "detail": "no pending inbound orders"})
    return checks
