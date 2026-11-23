"""End-to-end make-up billing tests on SQLite (run where backend deps exist).

Run inside the backend container or a venv with requirements installed:
    python3 -m unittest tests.test_billing_integration -v
"""

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.constants.enums import AdjustmentStatus, SettlementOutcome
from src.domain import billing
from src.models.base import Base
from src.models.workflow import (
    Adjustment, AnalysisRun, Bill, LateReading, Observation, SettlementAttempt,
)
from src.services import billing as service
from src.services.errors import BusinessError

RATE_PARAMETERS = {"valley_rate": 4.2, "flat_rate": 6.8, "peak_rate": 10.6}
ROW = {
    "pool_zone": "岩露池区", "meter_code": "XQ-R01",
    "start_date": "2026-09-01", "start_minute": 900,
    "end_date": "2026-09-02", "end_minute": 0,
    "reading_start_m3": 1211.660, "reading_end_m3": 1228.080,
}


class BillingIntegrationTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(engine)
        with self.Session.begin() as db:
            observation = Observation(source_key="data:row-1", payload=ROW)
            db.add(observation)
            db.flush()
            run = AnalysisRun(title="9月上旬透视", owner="analyst@demo.local",
                              parameters=RATE_PARAMETERS, last_observation_id=observation.id,
                              sample_size=1, observation_ids=[observation.id],
                              status="succeeded", result={})
            db.add(run)
            db.flush()
            self.run_id = run.id
        self.analyst = "analyst@demo.local"
        self.clerk = "supervisor@demo.local"

    def _create_bill(self, db):
        return service.create_bill(
            db, SimpleNamespace(run_id=self.run_id, title="9月热水账单"), self.analyst)

    def _add_reading(self, db, bill_id, minute, reading_m3):
        return service.add_late_reading(db, bill_id, SimpleNamespace(
            meter_code="XQ-R01", reading_date="2026-09-01", reading_minute=minute,
            reading_m3=reading_m3, note=""), self.analyst)

    def test_bill_freezes_original_amount_and_rates(self):
        with self.Session.begin() as db:
            bill = self._create_bill(db)
            stored = db.get(Bill, bill.id)
            self.assertEqual(stored.original_view["total_cost_yuan"], 136.83)
            self.assertEqual(stored.rates, billing.parse_rates(RATE_PARAMETERS))
            # Original rows are frozen independently of later changes.
            self.assertEqual(len(stored.original_rows), 1)
            self.assertTrue(stored.original_view == stored.latest_view)

    def test_pending_balance_tracks_new_readings_and_reconciles(self):
        with self.Session.begin() as db:
            bill = self._create_bill(db)
            self._add_reading(db, bill.id, 1080, 1220.000)
            detail = service.bill_detail(db, bill.id)
            self.assertEqual(detail["pending_adjustment_yuan"], -1.14)
            self.assertEqual(detail["balance_due_yuan"], -1.14)
            self.assertTrue(detail["reconciled"])
            self.assertAlmostEqual(detail["latest"]["total_cost_yuan"], 136.83 - 1.14, places=2)
            # The pending adjustment already points at its source meter bottom.
            adjustment = detail["adjustments"][0]
            self.assertEqual(len(adjustment["late_readings"]), 1)
            self.assertEqual(adjustment["late_readings"][0]["meter_code"], "XQ-R01")

    def test_submitted_amount_is_frozen_while_pending_reflects_later_reading(self):
        with self.Session.begin() as db:
            bill = self._create_bill(db)
            self._add_reading(db, bill.id, 1080, 1220.000)
            first = service.submit_adjustment(db, bill.id, self.analyst)
            first_amount = first.amount_cents
            first_id = first.id
            # A second late bottom arrives while the first make-up is with the clerk.
            self._add_reading(db, bill.id, 1320, 1225.000)
            detail = service.bill_detail(db, bill.id)
            submitted = next(a for a in detail["adjustments"] if a["id"] == first_id)
            pending = next(a for a in detail["adjustments"] if a["status"] == AdjustmentStatus.PENDING)
            self.assertEqual(submitted["amount_yuan"], first_amount / 100)
            self.assertEqual(pending["amount_yuan"], -2.48)
            self.assertEqual(detail["submitted_adjustment_yuan"], first_amount / 100)
            self.assertEqual(detail["balance_due_yuan"], round(-1.14 - 2.48, 2))
            self.assertTrue(detail["reconciled"])
            self.assertAlmostEqual(detail["latest"]["total_cost_yuan"], 136.83 - 1.14 - 2.48, places=2)

    def test_clerk_settles_frozen_amount_then_retry_is_idempotent(self):
        with self.Session.begin() as db:
            bill = self._create_bill(db)
            self._add_reading(db, bill.id, 1080, 1220.000)
            adjustment = service.submit_adjustment(db, bill.id, self.analyst)
            adjustment_id = adjustment.id
            frozen_amount = adjustment.amount_cents
            # First checkout fails; no money is posted.
            _adj, failed, handled = service.settle_adjustment(
                db, adjustment_id,
                SimpleNamespace(idempotency_key="key-attempt-1", force_failure=True), self.clerk)
            self.assertTrue(handled)
            self.assertEqual(failed.outcome, SettlementOutcome.FAILED)
            self.assertEqual(db.get(Adjustment, adjustment_id).status, AdjustmentStatus.SUBMITTED)
            # Retrying with the same key returns the recorded attempt and posts nothing again.
            _adj, replay, handled = service.settle_adjustment(
                db, adjustment_id,
                SimpleNamespace(idempotency_key="key-attempt-1", force_failure=False), self.clerk)
            self.assertFalse(handled)
            self.assertEqual(replay.outcome, SettlementOutcome.FAILED)
            self.assertEqual(db.query(SettlementAttempt).count(), 1)
            # A fresh key completes the checkout, still at the originally frozen amount.
            adj, success, handled = service.settle_adjustment(
                db, adjustment_id,
                SimpleNamespace(idempotency_key="key-attempt-2", force_failure=False), self.clerk)
            self.assertTrue(handled)
            self.assertEqual(success.outcome, SettlementOutcome.SUCCESS)
            self.assertEqual(adj.status, AdjustmentStatus.SETTLED)
            self.assertEqual(adj.amount_cents, frozen_amount)
            self.assertEqual(db.query(SettlementAttempt).count(), 2)
            # Retrying a settled adjustment with a new key is refused.
            with self.assertRaises(BusinessError):
                service.settle_adjustment(
                    db, adjustment_id,
                    SimpleNamespace(idempotency_key="key-attempt-3", force_failure=False), self.clerk)
            # Replaying the successful key stays idempotent and does not add a row.
            _adj, again, handled = service.settle_adjustment(
                db, adjustment_id,
                SimpleNamespace(idempotency_key="key-attempt-2", force_failure=False), self.clerk)
            self.assertFalse(handled)
            self.assertEqual(db.query(SettlementAttempt).count(), 2)

    def test_reconciliation_original_plus_makeups_equals_latest(self):
        with self.Session.begin() as db:
            bill = self._create_bill(db)
            self._add_reading(db, bill.id, 1080, 1220.000)
            service.submit_adjustment(db, bill.id, self.analyst)
            settled_id = db.query(Adjustment).one().id
            service.settle_adjustment(
                db, settled_id,
                SimpleNamespace(idempotency_key="settle-1", force_failure=False), self.clerk)
            self._add_reading(db, bill.id, 1320, 1225.000)
            detail = service.bill_detail(db, bill.id)
            self.assertTrue(detail["reconciled"])
            self.assertAlmostEqual(
                detail["original_total_yuan"]
                + detail["settled_adjustment_yuan"]
                + detail["submitted_adjustment_yuan"]
                + detail["pending_adjustment_yuan"],
                detail["latest_total_yuan"], places=6)
            # Every make-up traces back to a concrete meter bottom.
            for adjustment in detail["adjustments"]:
                self.assertTrue(adjustment["late_readings"])
                for reading in adjustment["late_readings"]:
                    self.assertIsNotNone(reading["id"])

    def test_duplicate_and_out_of_range_bottoms_are_rejected(self):
        with self.Session.begin() as db:
            bill = self._create_bill(db)
            self._add_reading(db, bill.id, 1080, 1220.000)
            with self.assertRaises(BusinessError):
                self._add_reading(db, bill.id, 1080, 1221.000)
            with self.assertRaises(BusinessError):
                self._add_reading(db, bill.id, 900, 1211.660)
            with self.assertRaises(BusinessError):
                self._add_reading(db, bill.id, 1080, 1299.999)
            # No orphan pending adjustment should carry the rejected bottoms.
            readings = db.query(LateReading).count()
            self.assertEqual(readings, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
