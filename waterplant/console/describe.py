"""Human readable component summaries."""

from __future__ import annotations

from waterplant.ns import treatment_line
from waterplant.store import describe_store

from .runtime import Runtime


def collect(rt: Runtime) -> dict[str, str]:
    """Return one description line per component."""

    return {
        "pipeline": treatment_line().describe(),
        "store": describe_store(rt.store),
        "intake": rt.flow_repository.describe(),
        "coag": rt.coag_doser.describe(),
        "chlor": rt.chlor_doser.describe(),
        "filter": rt.bank.describe(),
        "backwash": rt.backwash.describe(),
        "turbidity": rt.sampler.describe(),
        "flow": rt.calibration.describe(),
        "clearwell": rt.well.describe(),
        "quota": rt.accumulator.describe(),
        "audit": rt.auditor.describe(),
        "ph": rt.stabilizer.describe(),
        "schedule": rt.scheduler.describe(rt.bank),
        "trend": rt.trend.describe(),
        "inventory": rt.inventory.describe(),
        "batches": _describe_batches(rt),
    }


def _describe_batches(rt: Runtime) -> str:
    state = rt.batch_service.state()
    kinds = [alert["kind"] for alert in state["alerts"]]
    return (
        f"batches policy={state['consumption_policy'].split(' ')[0]} "
        f"chemicals={len(state['chemicals'])} alerts={kinds} "
        f"quarantine_pending={state['quarantine_pending']} "
        f"inbound_pending={state['inbound_pending']}"
    )
