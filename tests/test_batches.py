"""Behavioural tests for the batch inventory subsystem."""

from __future__ import annotations

import tempfile
import threading
import unittest

from waterplant.batches import (
    Clock,
    InboundLine,
    BatchService,
    IdempotencyConflict,
    QuarantineConflict,
    fefo_sort_key,
)
from waterplant.batches.models import Batch, ACTIVE, EXHAUSTED, EXPIRED
from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.store import Store


def line(chemical: str, amount: float, expiry: str = "", supplier: str = "") -> InboundLine:
    return InboundLine(chemical=chemical, amount=amount, expiry_date=expiry, supplier_ref=supplier)


def open_service(fixed_day: str = "2026-10-05") -> tuple[str, Store, BatchService]:
    tmp = tempfile.mkdtemp()
    store = Store.open(f"{tmp}/state.json")
    service = BatchService(store, Clock())
    service.clock.set_today(fixed_day)
    return tmp, store, service


class FefoOrderingCase(unittest.TestCase):
    def test_earliest_expiry_is_drawn_first_regardless_of_arrival(self) -> None:
        _, store, service = open_service()
        # New stock arrives first but expires later; older dated stock arrives second.
        service.submit_inbound([line("chlorine", 50.0, "2027-01-01")], "r1")
        service.submit_inbound([line("chlorine", 50.0, "2026-11-01")], "r2")
        result = service.consume("chlorine", 60.0, "d1")
        allocations = [(a.batch_id.rsplit("-", 1)[0], a.quantity) for a in result.allocations]
        batches = {batch.batch_id: batch for batch in service.list_batches("chlorine")}
        short_expiry = [b for b in batches.values() if b.expiry_date == "2026-11-01"][0]
        long_expiry = [b for b in batches.values() if b.expiry_date == "2027-01-01"][0]
        self.assertEqual(short_expiry.consumed, 50.0)
        self.assertEqual(long_expiry.consumed, 10.0)
        self.assertEqual(len(allocations), 2)

    def test_equal_expiry_falls_back_to_fifo_arrival_sequence(self) -> None:
        _, store, service = open_service()
        first = service.submit_inbound([line("pac", 20.0, "2026-12-01")], "a")
        second = service.submit_inbound([line("pac", 20.0, "2026-12-01")], "b")
        result = service.consume("pac", 20.0, "use")
        self.assertEqual(result.allocations[0].batch_id, first.order.batch_ids[0])
        self.assertNotEqual(result.allocations[0].batch_id, second.order.batch_ids[0])

    def test_sort_key_is_deterministic(self) -> None:
        older = Batch(chemical="c", batch_id="b1", received=1.0, expiry_date="2026-11-01", seq=2)
        newer = Batch(chemical="c", batch_id="b2", received=1.0, expiry_date="2026-12-01", seq=1)
        self.assertEqual(sorted([newer, older], key=fefo_sort_key), [older, newer])

    def test_expired_stock_is_never_consumed_but_remains_counted_separately(self) -> None:
        _, store, service = open_service("2026-10-05")
        service.submit_inbound([line("chlorine", 30.0, "2026-09-01")], "old")
        service.submit_inbound([line("chlorine", 30.0, "2026-12-01")], "fresh")
        result = service.consume("chlorine", 30.0, "use")
        self.assertEqual(result.allocations[0].expiry_date, "2026-12-01")
        self.assertEqual(service.balance("chlorine"), 0.0)
        self.assertEqual(service.balance("chlorine", include_expired=True), 30.0)
        with self.assertRaises(ValueError):
            service.consume("chlorine", 1.0, "use2")


class AllOrNothingCase(unittest.TestCase):
    def test_short_consumption_writes_nothing(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 10.0, "2026-12-01")], "in")
        with self.assertRaises(ValueError):
            service.consume("chlorine", 20.0, "big")
        # No batches touched, no order, no ledger out, no idempotency reservation.
        self.assertEqual(service.balance("chlorine"), 10.0)
        self.assertEqual(service.list_consumptions(), [])
        self.assertEqual(
            [entry for entry in service.list_ledger() if entry.movement == "out"], []
        )
        # The failed request id can be reused because nothing was committed.
        result = service.consume("chlorine", 5.0, "big")
        self.assertEqual(result.order.amount, 5.0)

    def test_rollback_discards_staged_writes(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 10.0, "2026-12-01")], "in")
        before_keys = set(store.keys())
        with self.assertRaises(ValueError):
            service.consume("chlorine", 99.0, "x")
        self.assertEqual(set(store.keys()), before_keys)


class IdempotencyCase(unittest.TestCase):
    def test_identical_inbound_resend_replays_without_double_booking(self) -> None:
        _, store, service = open_service()
        payload = [line("chlorine", 40.0, "2026-12-01")]
        first = service.submit_inbound(payload, "dup")
        second = service.submit_inbound(payload, "dup")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.order.order_id, second.order.order_id)
        self.assertEqual(len(service.list_batches("chlorine")), 1)
        self.assertEqual(service.balance("chlorine"), 40.0)
        kinds = [entry.kind for entry in service.auditor.entries()]
        self.assertNotIn("inbound.quarantine", kinds)

    def test_consumption_resend_does_not_deduct_twice(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 100.0, "2026-12-01")], "in")
        first = service.consume("chlorine", 40.0, "dose")
        second = service.consume("chlorine", 40.0, "dose")
        self.assertTrue(second.replayed)
        self.assertEqual(first.order.order_id, second.order.order_id)
        self.assertEqual(service.balance("chlorine"), 60.0)
        outbound = [entry for entry in service.list_ledger() if entry.movement == "out"]
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].quantity, 40.0)

    def test_same_request_id_different_op_conflicts(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 10.0, "2026-12-01")], "shared")
        with self.assertRaises(IdempotencyConflict):
            service.consume("chlorine", 1.0, "shared")

    def test_same_consumption_request_id_different_amount_conflicts(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 100.0, "2026-12-01")], "in")
        service.consume("chlorine", 10.0, "dose")
        with self.assertRaises(IdempotencyConflict):
            service.consume("chlorine", 11.0, "dose")
        self.assertEqual(service.balance("chlorine"), 90.0)

    def test_request_id_is_required(self) -> None:
        _, store, service = open_service()
        with self.assertRaises(ValueError):
            service.submit_inbound([line("chlorine", 10.0, "2026-12-01")], "")
        with self.assertRaises(ValueError):
            service.consume("chlorine", 10.0, "")


class QuarantineCase(unittest.TestCase):
    def test_conflicting_resend_is_parked_not_dropped_or_booked(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 40.0, "2026-12-01")], "doc")
        with self.assertRaises(QuarantineConflict) as caught:
            service.submit_inbound([line("chlorine", 99.0, "2027-01-01")], "doc")
        quarantine_id = caught.exception.quarantine_id
        # Original stock only; conflicting delivery not booked.
        self.assertEqual(service.balance("chlorine"), 40.0)
        pending = service.quarantine_entries("pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].quarantine_id, quarantine_id)

    def test_repeated_conflict_is_idempotent_single_quarantine_row(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 40.0, "2026-12-01")], "doc")
        conflicting = [line("chlorine", 99.0, "2027-01-01")]
        for _ in range(3):
            with self.assertRaises(QuarantineConflict):
                service.submit_inbound(conflicting, "doc")
        self.assertEqual(len(service.quarantine_entries("pending")), 1)

    def test_manual_approval_books_conflicting_delivery(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 40.0, "2026-12-01")], "doc")
        with self.assertRaises(QuarantineConflict) as caught:
            service.submit_inbound([line("chlorine", 99.0, "2027-01-01")], "doc")
        decision = service.resolve_quarantine(caught.exception.quarantine_id, True, "warehouse confirmed")
        self.assertEqual(decision["decision"], "approved")
        self.assertEqual(service.balance("chlorine"), 40.0 + 99.0)
        self.assertEqual(service.quarantine_entries("pending"), [])
        resolved = service.quarantine_entries()[0]
        self.assertEqual(resolved.status, "approved")

    def test_manual_rejection_discards_but_keeps_audit(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 40.0, "2026-12-01")], "doc")
        with self.assertRaises(QuarantineConflict) as caught:
            service.submit_inbound([line("chlorine", 99.0, "2027-01-01")], "doc")
        decision = service.resolve_quarantine(caught.exception.quarantine_id, False, "typo")
        self.assertEqual(decision["decision"], "rejected")
        self.assertEqual(service.balance("chlorine"), 40.0)
        kinds = [entry.kind for entry in service.auditor.entries()]
        self.assertIn("inbound.quarantine.reject", kinds)
        with self.assertRaises(ValueError):
            service.resolve_quarantine(caught.exception.quarantine_id, True)


    def test_concurrent_identical_inbound_only_books_once(self) -> None:
        _, store, service = open_service()
        results: list[object] = []
        errors: list[Exception] = []
        lock = threading.Lock()
        payload = [line("chlorine", 40.0, "2026-12-01")]

        def worker() -> None:
            try:
                result = service.submit_inbound(payload, "race")
                with lock:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001 - asserted below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        order_ids = {result.order.order_id for result in results}
        self.assertEqual(len(order_ids), 1)
        self.assertEqual(service.balance("chlorine"), 40.0)
        self.assertEqual(len(service.list_batches("chlorine")), 1)


class RecoveryCase(unittest.TestCase):
    def test_received_order_is_recovered_without_double_booking(self) -> None:
        tmp, store, service = open_service()
        service.submit_inbound([line("chlorine", 40.0, "2026-12-01")], "r1")
        # Simulate a crash after phase one: hand craft a stranded received order.
        from waterplant.batches.models import InboundOrder
        from waterplant.batches.repo import InboundRepository
        stranded = InboundOrder(
            order_id="IN_stranded",
            request_id="stranded-req",
            lines=[line("chlorine", 25.0, "2027-01-01")],
            status="received",
            fingerprint="x",
            created_at=service.clock.now(),
        )
        repo = InboundRepository(store)
        orders = repo.load()
        orders.append(stranded)
        repo.save(orders)

        recovered = service.recover()
        self.assertEqual(recovered, ["IN_stranded"])
        self.assertEqual(service.balance("chlorine"), 40.0 + 25.0)
        # Recovery is idempotent: running it again changes nothing.
        self.assertEqual(service.recover(), [])
        self.assertEqual(service.balance("chlorine"), 40.0 + 25.0)
        inbound = {order.order_id: order for order in service.list_inbounds()}
        self.assertEqual(inbound["IN_stranded"].status, "applied")
        self.assertEqual(len(inbound["IN_stranded"].batch_ids), 1)


class ExpiryAndAlertCase(unittest.TestCase):
    def test_near_expiry_and_expired_alerts(self) -> None:
        _, store, service = open_service("2026-10-05")
        service.set_near_expiry_days(30)
        service.submit_inbound([line("chlorine", 10.0, "2026-10-20")], "near")
        service.submit_inbound([line("chlorine", 10.0, "2026-10-01")], "gone")
        service.submit_inbound([line("pac", 10.0, "2027-01-01")], "fresh")
        kinds = {alert.batch_id or alert.chemical: alert.kind for alert in service.alerts()}
        by_expiry = {batch.expiry_date: batch for batch in service.list_batches("chlorine")}
        self.assertIn(by_expiry["2026-10-01"].batch_id, [
            alert.batch_id for alert in service.alerts() if alert.kind == "expired"
        ])
        self.assertIn(by_expiry["2026-10-20"].batch_id, [
            alert.batch_id for alert in service.alerts() if alert.kind == "near_expiry"
        ])

    def test_refresh_flips_batches_and_is_idempotent(self) -> None:
        _, store, service = open_service("2026-10-05")
        service.submit_inbound([line("chlorine", 10.0, "2026-10-10")], "r")
        self.assertEqual(service.refresh_expiry_status(), [])
        service.clock.set_today("2026-10-11")
        changed = service.refresh_expiry_status()
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].status, EXPIRED)
        # Second run does not re-audit the same batch.
        self.assertEqual(service.refresh_expiry_status(), [])
        expires = [entry for entry in service.auditor.entries() if entry.kind == "batch.expire"]
        self.assertEqual(len(expires), 1)

    def test_low_stock_alert_uses_per_chemical_level(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 5.0, "2027-01-01")], "r")
        service.set_reorder_level("chlorine", 50.0)
        low = [alert for alert in service.alerts() if alert.kind == "low_stock"]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0].chemical, "chlorine")

    def test_scrap_writes_off_expired_stock_with_audit(self) -> None:
        _, store, service = open_service("2026-10-05")
        result = service.submit_inbound([line("chlorine", 30.0, "2026-09-01")], "old")
        batch_id = result.order.batch_ids[0]
        outcome = service.scrap_batch(batch_id, reason="expired disposal")
        self.assertAlmostEqual(outcome["scrapped"], 30.0)
        self.assertEqual(service.balance("chlorine", include_expired=True), 0.0)
        rows = [entry for entry in service.list_ledger() if entry.movement == "scrap"]
        self.assertEqual(len(rows), 1)
        scraps = [entry for entry in service.auditor.entries() if entry.kind == "batch.scrap"]
        self.assertEqual(len(scraps), 1)


class LedgerConsistencyCase(unittest.TestCase):
    def test_ledger_movements_reconstruct_balance(self) -> None:
        _, store, service = open_service()
        service.submit_inbound(
            [line("chlorine", 100.0, "2026-12-01"), line("pac", 40.0, "2027-01-01")], "in1"
        )
        service.submit_inbound([line("chlorine", 20.0, "2026-11-01")], "in2")
        service.consume("chlorine", 110.0, "d1", purpose="dosing")
        service.consume("pac", 5.0, "d2")
        totals_in: dict[str, float] = {}
        totals_out: dict[str, float] = {}
        totals_scrap: dict[str, float] = {}
        for entry in service.list_ledger():
            if entry.movement == "in":
                totals_in[entry.chemical] = totals_in.get(entry.chemical, 0.0) + entry.quantity
            elif entry.movement == "out":
                totals_out[entry.chemical] = totals_out.get(entry.chemical, 0.0) + entry.quantity
            elif entry.movement == "scrap":
                totals_scrap[entry.chemical] = totals_scrap.get(entry.chemical, 0.0) + entry.quantity
        self.assertEqual(totals_in["chlorine"], 120.0)
        self.assertEqual(totals_out["chlorine"], 110.0)
        self.assertEqual(service.balance("chlorine"), 10.0)
        self.assertEqual(
            service.balance("chlorine"),
            totals_in["chlorine"] - totals_out.get("chlorine", 0.0) - totals_scrap.get("chlorine", 0.0),
        )
        # Every out movement references a real, applied batch and request.
        inbound_batches = {batch.batch_id for batch in service.list_batches()}
        for entry in service.list_ledger():
            if entry.movement in ("out", "in", "scrap"):
                self.assertIn(entry.batch_id, inbound_batches)

    def test_concurrent_consumptions_never_oversell_a_batch(self) -> None:
        _, store, service = open_service()
        service.submit_inbound([line("chlorine", 100.0, "2026-12-01")], "stock")
        outcomes: list[bool] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                service.consume("chlorine", 30.0, f"dose-{index}")
                ok = True
            except ValueError:
                ok = False
            with lock:
                outcomes.append(ok)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Exactly three 30-unit withdrawals can succeed against 100 on hand.
        self.assertEqual(sum(outcomes), 3)
        self.assertAlmostEqual(service.balance("chlorine"), 10.0)
        outbound = [entry for entry in service.list_ledger() if entry.movement == "out"]
        self.assertAlmostEqual(sum(entry.quantity for entry in outbound), 90.0)


class ConsoleRouteCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)
        self.server.runtime.clock.set_today("2026-10-05")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        import json

        response = self.server.dispatch(method, path, payload)
        body = json.loads(response.body.decode("utf-8"))
        return response.status, body

    def test_inbound_consume_quarantine_flow_over_http(self) -> None:
        status, body = self.call(
            "POST",
            "/batches/inbound",
            {
                "request_id": "w1",
                "lines": [
                    {"chemical": "chlorine", "amount": 50.0, "expiry_date": "2026-11-01"},
                    {"chemical": "pac", "amount": 20.0, "expiry_date": "2027-01-01"},
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "applied")
        self.assertEqual(len(body["batch_ids"]), 2)

        # Identical resend replays.
        status, body = self.call(
            "POST",
            "/batches/inbound",
            {"request_id": "w1", "lines": [
                {"chemical": "chlorine", "amount": 50.0, "expiry_date": "2026-11-01"},
                {"chemical": "pac", "amount": 20.0, "expiry_date": "2027-01-01"},
            ]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["replayed"])

        # Conflicting resend is 409 + quarantined.
        status, body = self.call(
            "POST",
            "/batches/inbound",
            {"request_id": "w1", "lines": [
                {"chemical": "chlorine", "amount": 999.0, "expiry_date": "2028-01-01"}
            ]},
        )
        self.assertEqual(status, 409)
        self.assertIn("parked", body["error"])

        status, body = self.call("GET", "/batches/quarantine", {"status": "pending"})
        self.assertEqual(status, 200)
        quarantine_id = body["entries"][0]["quarantine_id"]

        status, body = self.call(
            "POST",
            "/batches/quarantine/resolve",
            {"quarantine_id": quarantine_id, "decision": "reject", "note": "bad resend"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"], "rejected")

        status, body = self.call(
            "POST", "/batches/consume", {"request_id": "d1", "chemical": "chlorine", "amount": 50.0}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["consumed"], 50.0)
        self.assertEqual(body["allocations"][0]["expiry_date"], "2026-11-01")

        status, body = self.call(
            "POST", "/batches/consume", {"request_id": "d1", "chemical": "chlorine", "amount": 50.0}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["replayed"])

        status, body = self.call("GET", "/batches", {})
        self.assertEqual(status, 200)
        self.assertIn("consumption_policy", body)
        self.assertEqual(body["balances"]["pac"]["usable"], 20.0)

        status, body = self.call("GET", "/batches/audit", {})
        self.assertEqual(status, 200)
        kinds = {entry["kind"] for entry in body["entries"]}
        self.assertIn("batch.receive", kinds)
        self.assertIn("batch.consume", kinds)
        self.assertIn("inbound.quarantine.reject", kinds)

    def test_insufficient_stock_returns_400_and_checks_surface_alerts(self) -> None:
        self.call(
            "POST",
            "/batches/inbound",
            {"request_id": "r", "lines": [{"chemical": "chlorine", "amount": 5.0, "expiry_date": "2027-01-01"}]},
        )
        status, body = self.call(
            "POST", "/batches/consume", {"request_id": "x", "chemical": "chlorine", "amount": 50.0}
        )
        self.assertEqual(status, 400)
        self.assertIn("insufficient", body["error"])
        status, checks = self.call("GET", "/health/checks", {})
        names = {check["name"]: check for check in checks["checks"]}
        self.assertIn("batches-low-stock", names)


if __name__ == "__main__":
    unittest.main()
