"""Console HTTP handlers for the batch inventory subsystem."""

from __future__ import annotations

from waterplant.batches import (
    IdempotencyConflict,
    InboundLine,
    QuarantineConflict,
)

from .http import Request, RequestError, json_response
from .runtime import Runtime


def _service(rt: Runtime):
    return rt.batch_service


def _lines_from_payload(request: Request) -> list[InboundLine]:
    raw = request.payload.get("lines")
    if raw is None:
        # Convenience form: a single line supplied at the top level.
        if not request.str_field("chemical"):
            raise RequestError(400, "field lines must be a list, or chemical/amount given")
        raw = [
            {
                "chemical": request.str_field("chemical"),
                "amount": request.float_field("amount"),
                "expiry_date": request.str_field("expiry_date"),
                "supplier_ref": request.str_field("supplier_ref"),
            }
        ]
    if not isinstance(raw, list) or not raw:
        raise RequestError(400, "field lines must be a non-empty list")
    lines: list[InboundLine] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RequestError(400, f"line {index} must be an object")
        chemical = str(item.get("chemical", "")).strip()
        if not chemical:
            raise RequestError(400, f"line {index} chemical must not be empty")
        lines.append(
            InboundLine(
                chemical=chemical,
                amount=float(item.get("amount", 0.0)),
                expiry_date=str(item.get("expiry_date", "") or ""),
                supplier_ref=str(item.get("supplier_ref", "") or ""),
            )
        )
    return lines


def _request_id(request: Request) -> str:
    request_id = request.str_field("request_id").strip()
    if not request_id:
        raise RequestError(400, "request_id is required for idempotent submissions")
    return request_id


def batch_state(rt: Runtime, request: Request) -> object:
    return json_response(_service(rt).state())


def batch_list(rt: Runtime, request: Request) -> object:
    chemical = request.str_field("chemical")
    today = _service(rt).clock.today()
    return json_response(
        {"batches": [batch.view(today) for batch in _service(rt).list_batches(chemical)]}
    )


def inbound_submit(rt: Runtime, request: Request) -> object:
    service = _service(rt)
    lines = _lines_from_payload(request)
    request_id = _request_id(request)
    order_id = request.str_field("order_id")
    try:
        result = service.submit_inbound(lines, request_id, order_id)
    except QuarantineConflict as exc:
        raise RequestError(409, str(exc)) from exc
    except IdempotencyConflict as exc:
        raise RequestError(409, str(exc)) from exc
    return json_response(result.as_dict(service.clock.today()))


def inbound_list(rt: Runtime, request: Request) -> object:
    return json_response({"orders": [order.as_dict() for order in _service(rt).list_inbounds()]})


def inbound_recover(rt: Runtime, request: Request) -> object:
    recovered = _service(rt).recover()
    return json_response({"recovered": recovered, "count": len(recovered)})


def consume_submit(rt: Runtime, request: Request) -> object:
    service = _service(rt)
    chemical = request.str_field("chemical")
    amount = request.float_field("amount")
    request_id = _request_id(request)
    order_id = request.str_field("order_id")
    purpose = request.str_field("purpose")
    try:
        result = service.consume(chemical, amount, request_id, order_id, purpose)
    except IdempotencyConflict as exc:
        raise RequestError(409, str(exc)) from exc
    return json_response(result.as_dict())


def consumption_list(rt: Runtime, request: Request) -> object:
    chemical = request.str_field("chemical")
    return json_response(
        {"orders": [order.as_dict() for order in _service(rt).list_consumptions(chemical)]}
    )


def scrap_submit(rt: Runtime, request: Request) -> object:
    service = _service(rt)
    batch_id = request.str_field("batch_id")
    if not batch_id:
        raise RequestError(400, "batch_id is required")
    raw_amount = request.payload.get("amount")
    amount = None if raw_amount is None else float(raw_amount)
    request_id = request.str_field("request_id")
    reason = request.str_field("reason")
    try:
        result = service.scrap_batch(batch_id, amount, request_id, reason)
    except IdempotencyConflict as exc:
        raise RequestError(409, str(exc)) from exc
    return json_response(result)


def expiry_refresh(rt: Runtime, request: Request) -> object:
    changed = _service(rt).refresh_expiry_status()
    today = _service(rt).clock.today()
    return json_response(
        {"expired": [batch.view(today) for batch in changed], "count": len(changed)}
    )


def alerts_list(rt: Runtime, request: Request) -> object:
    return json_response({"alerts": [alert.as_dict() for alert in _service(rt).alerts()]})


def quarantine_list(rt: Runtime, request: Request) -> object:
    status = request.str_field("status")
    return json_response(
        {"entries": [entry.as_dict() for entry in _service(rt).quarantine_entries(status)]}
    )


def quarantine_resolve(rt: Runtime, request: Request) -> object:
    quarantine_id = request.str_field("quarantine_id")
    if not quarantine_id:
        raise RequestError(400, "quarantine_id is required")
    decision = request.str_field("decision", "approve").lower()
    if decision not in ("approve", "reject"):
        raise RequestError(400, "decision must be approve or reject")
    note = request.str_field("note")
    result = _service(rt).resolve_quarantine(quarantine_id, decision == "approve", note)
    return json_response(result)


def ledger_list(rt: Runtime, request: Request) -> object:
    chemical = request.str_field("chemical")
    batch_id = request.str_field("batch_id")
    return json_response(
        {"entries": [entry.as_dict() for entry in _service(rt).list_ledger(chemical, batch_id)]}
    )


def batch_audit_list(rt: Runtime, request: Request) -> object:
    kind = request.str_field("kind")
    chemical = request.str_field("chemical")
    batch_id = request.str_field("batch_id")
    return json_response(
        {
            "entries": [
                entry.as_dict()
                for entry in _service(rt).auditor.filter(kind, chemical, batch_id)
            ]
        }
    )


def near_expiry_config(rt: Runtime, request: Request) -> object:
    if request.method == "POST":
        days = request.int_field("days")
        return json_response({"near_expiry_days": _service(rt).set_near_expiry_days(days)})
    return json_response({"near_expiry_days": _service(rt).near_expiry_days()})


def reorder_config(rt: Runtime, request: Request) -> object:
    service = _service(rt)
    chemical = request.str_field("chemical")
    if not chemical:
        raise RequestError(400, "chemical is required")
    if request.method == "POST":
        level = request.float_field("level")
        return json_response({"chemical": chemical, "reorder_level": service.set_reorder_level(chemical, level)})
    return json_response({"chemical": chemical, "reorder_level": service.reorder_level(chemical)})
