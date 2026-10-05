"""Behaviour tests for batch inventory, expiry alerts and idempotency."""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from waterplant.audit import AUDIT_KEY, Auditor
from waterplant.inventory import (
    IDEMPOTENCY_KEY,
    LOTS_KEY,
    MOVEMENTS_KEY,
    RECEIPTS_KEY,
    InsufficientInventory,
    Inventory,
    InventoryConflict,
)
from waterplant.store import Store


class BatchInventoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.inventory = Inventory(self.store)
        self.today = date(2030, 1, 10)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def items(self) -> list[dict[str, object]]:
        return [
            {
                "chemical": "chlorine",
                "lot_id": "A",
                "amount": 5.0,
                "received_on": "2030-01-01",
                "expires_on": "2030-01-20",
            },
            {
                "chemical": "chlorine",
                "lot_id": "B",
                "amount": 10.0,
                "received_on": "2030-01-02",
                "expires_on": "2030-02-01",
            },
            {
                "chemical": "chlorine",
                "lot_id": "OLD",
                "amount": 7.0,
                "received_on": "2029-12-01",
                "expires_on": "2030-01-05",
            },
        ]

    def test_receipt_fefo_consumption_blocks_expired_stock(self) -> None:
        receipt = self.inventory.submit_receipt("GRN-1", self.items(), as_of=self.today)
        self.assertEqual(receipt["status"], "received")
        state = self.inventory.state(as_of=self.today)
        self.assertEqual(state.strategy, "FEFO_EXPIRY_FIRST__FIFO_TIEBREAK")
        self.assertEqual(state.balance, 22.0)
        self.assertEqual(state.available_balance, 15.0)
        self.assertEqual([lot["lot_id"] for lot in state.lots], ["A", "B", "OLD"])
        self.assertEqual(
            {
                (alert["type"], alert.get("lot_id"))
                for alert in state.alerts
                if alert.get("chemical") == "chlorine"
            },
            {("near_expiry", "A"), ("near_expiry", "B"), ("expired", "OLD"), ("low_stock", None)},
        )

        result = self.inventory.dispense("chlorine", 8.0, "dose-1", as_of=self.today)
        self.assertEqual(
            [(item["lot_id"], item["amount"]) for item in result["allocations"]],
            [("A", 5.0), ("B", 3.0)],
        )
        lots = {lot.lot_id: lot for lot in self.inventory.lots("chlorine", self.today)}
        self.assertEqual(lots["A"].consumed, 5.0)
        self.assertEqual(lots["B"].consumed, 3.0)
        self.assertEqual(lots["OLD"].consumed, 0.0)

        movements = self.inventory.movements()
        self.assertEqual([m["type"] for m in movements[:3]], ["receive", "receive", "receive"])
        self.assertEqual([m["lot_id"] for m in movements[3:]], ["A", "B"])
        dosing = [entry for entry in Auditor(self.store).entries() if entry.kind == "chlorine"]
        self.assertEqual(len(dosing), 1)
        self.assertEqual(dosing[0].ref, "dose-1")
        self.assertEqual(movements[3]["audit_id"], dosing[0].id)

    def test_same_expiry_uses_fifo_tie_break(self) -> None:
        items = [
            {
                "chemical": "coagulant",
                "lot_id": "EARLIER",
                "amount": 4.0,
                "received_on": "2030-01-01",
                "expires_on": "2030-02-01",
            },
            {
                "chemical": "coagulant",
                "lot_id": "LATER",
                "amount": 4.0,
                "received_on": "2030-01-03",
                "expires_on": "2030-02-01",
            },
        ]
        self.inventory.submit_receipt("GRN-FIFO", items, as_of=self.today)
        allocations = self.inventory.consume_allocations("coagulant", 3.0, "dose-fifo", self.today)
        self.assertEqual([item.lot_id for item in allocations], ["EARLIER"])

    def test_receipt_resend_is_idempotent_but_changed_payload_is_held(self) -> None:
        first = self.inventory.submit_receipt("GRN-2", self.items(), as_of=self.today)
        second = self.inventory.submit_receipt("GRN-2", self.items(), as_of=self.today)
        self.assertEqual(second["document_id"], first["document_id"])
        self.assertEqual(self.inventory.balance("chlorine"), 22.0)
        self.assertEqual(len(self.inventory.receipts()), 1)

        changed = self.items()
        changed[0]["amount"] = 6.0
        conflict = self.inventory.submit_receipt("GRN-2", changed, as_of=self.today)
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["conflict"]["warehouse_action"], "hold_for_manual_confirmation")
        self.assertEqual(self.inventory.balance("chlorine"), 22.0)

        discarded = self.inventory.discard_receipt("GRN-2", "duplicate manually rejected")
        self.assertEqual(discarded["status"], "discarded")
        with self.assertRaises(InventoryConflict):
            self.inventory.submit_receipt("GRN-2", changed, as_of=self.today)

    def test_recover_pending_partial_receipt_without_double_booking(self) -> None:
        self.inventory.submit_receipt("GRN-3", self.items(), as_of=self.today)

        # Simulate a crash after the first two lots were written but before the
        # document changed from pending to received.
        def simulate_crash(data: dict[str, str]) -> None:
            receipts = json.loads(data[RECEIPTS_KEY])
            receipts[0]["status"] = "pending"
            data[RECEIPTS_KEY] = json.dumps(receipts)
            lots = [lot for lot in json.loads(data[LOTS_KEY]) if lot["lot_id"] != "OLD"]
            data[LOTS_KEY] = json.dumps(lots)

        self.store.atomic(simulate_crash)
        recovered = self.inventory.recover_receipts(as_of=self.today)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "received")
        self.assertEqual(self.inventory.balance("chlorine"), 22.0)
        self.assertEqual(
            len([lot for lot in self.inventory.lots("chlorine") if lot.lot_id == "A"]),
            1,
        )

        self.inventory.recover_receipts(as_of=self.today)
        self.assertEqual(self.inventory.balance("chlorine"), 22.0)

    def test_invalid_pending_receipt_is_held_for_manual_review(self) -> None:
        self.inventory.submit_receipt("GRN-BAD", self.items(), as_of=self.today)

        def corrupt_pending(data: dict[str, str]) -> None:
            receipts = json.loads(data[RECEIPTS_KEY])
            receipts[0]["status"] = "pending"
            receipts[0]["items"] = []
            data[RECEIPTS_KEY] = json.dumps(receipts)

        self.store.atomic(corrupt_pending)
        recovered = self.inventory.recover_receipts(as_of=self.today)
        self.assertEqual(recovered[0]["status"], "conflict")
        self.assertEqual(
            recovered[0]["conflict"]["warehouse_action"],
            "hold_for_manual_confirmation",
        )

    def test_consumption_request_ids_are_idempotent_and_failure_is_atomic(self) -> None:
        self.inventory.submit_receipt("GRN-4", self.items(), as_of=self.today)
        self.inventory.dispense("chlorine", 8.0, "dose-2", as_of=self.today)
        cached = self.inventory.dispense("chlorine", 8.0, "dose-2", as_of=self.today)
        self.assertEqual(
            [item["lot_id"] for item in cached["allocations"]], ["A", "B"]
        )
        self.assertEqual(self.inventory.available_balance("chlorine", self.today), 7.0)
        self.assertEqual(len(Auditor(self.store).entries()), 1)

        with self.assertRaises(InventoryConflict):
            self.inventory.dispense("chlorine", 9.0, "dose-2", as_of=self.today)

        audit_count = len(Auditor(self.store).entries())
        with self.assertRaises(InsufficientInventory):
            self.inventory.dispense("chlorine", 8.0, "dose-fail", as_of=self.today)
        # The batch ledger, idempotency table and dosing audit roll back together.
        self.assertEqual(len(Auditor(self.store).entries()), audit_count)
        raw, _ = self.store.get(IDEMPOTENCY_KEY)
        self.assertEqual(len(json.loads(raw)), 1)
        self.assertEqual(len(self.inventory.movements()), 5)

    def test_near_expiry_window_and_sweep(self) -> None:
        self.inventory.set_expiry_warning_days(5)
        self.inventory.submit_receipt("GRN-5", self.items(), as_of=self.today)
        state = self.inventory.state(as_of=self.today)
        self.assertFalse(any(alert["type"] == "near_expiry" for alert in state.alerts))

        self.inventory.sweep_expired(as_of=self.today)
        expired = self.inventory.sweep_expired(as_of=date(2030, 2, 2))
        self.assertEqual({lot["lot_id"] for lot in expired}, {"A", "B"})
        self.assertEqual(self.inventory.available_balance("chlorine", date(2030, 2, 2)), 0.0)
        self.assertTrue(all(movement["type"] == "expire" for movement in self.inventory.movements()[-2:]))

    def test_concurrent_deductions_do_not_overspend_a_batch(self) -> None:
        items = [
            {
                "chemical": "chlorine",
                "lot_id": "Q1",
                "amount": 10.0,
                "received_on": "2030-01-01",
                "expires_on": "2030-02-01",
            }
        ]
        self.inventory.submit_receipt("GRN-Q", items, as_of=self.today)

        def consume(index: int) -> bool:
            try:
                self.inventory.dispense("chlorine", 3.0, f"q-{index}", as_of=self.today)
                return True
            except InsufficientInventory:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(consume, range(8)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(self.inventory.lots("chlorine", self.today)[0].consumed, 9.0)


if __name__ == "__main__":
    unittest.main()
