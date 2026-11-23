"""HTTP end-to-end test for the make-up billing feature (run where deps exist).

Exercises the real FastAPI routes, JWT role guards and the full business flow:
original bill retained, late reading splits an interval at original rates,
pending balance tracks new readings, submitted amount stays frozen, checkout
failure + idempotent retry never double-posts, and totals reconcile.

    python3 -m unittest tests.test_billing_api -v
"""

import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="billing-api-", suffix=".db")
os.close(_DB_FD)
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{_DB_PATH}")
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
os.environ["JWT_SECRET"] = "test-secret-with-enough-length-for-hs256-signing-2026"
os.environ["SEED_DEMO"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.main import app  # noqa: E402
from src.models import base as base_module  # noqa: E402
from src.models.base import Base, Session, engine  # noqa: E402
from src.models.workflow import AnalysisRun, Observation  # noqa: E402
from src.routes import session as session_route  # noqa: E402
from src.services import broker  # noqa: E402


class _FakePipeline:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def incr(self, _key):
        return self

    def expire(self, *_args, **_kwargs):
        return self

    def lpush(self, *_args, **_kwargs):
        return self

    def ltrim(self, *_args, **_kwargs):
        return self

    def execute(self):
        return [1, True]


class FakeRedis:
    def ping(self):
        return True

    def pipeline(self):
        return _FakePipeline()


# No real Redis is needed; the login route only uses rate-limit counters.
_REDIS = FakeRedis()
broker.redis = _REDIS
session_route.redis = _REDIS

ROW = {
    "pool_zone": "岩露池区", "meter_code": "XQ-R01",
    "start_date": "2026-09-01", "start_minute": 900,
    "end_date": "2026-09-02", "end_minute": 0,
    "reading_start_m3": 1211.660, "reading_end_m3": 1228.080,
}
PARAMETERS = {"valley_rate": 4.2, "flat_rate": 6.8, "peak_rate": 10.6}


class BillingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Bind the app's shared engine to a file-backed SQLite DB so every route
        # session (separate connections) sees the same tables and data. The route
        # modules captured Session at import time, so rebind it there as well.
        cls.engine = create_engine(f"sqlite+pysqlite:///{_DB_PATH}")
        cls.session_factory = sessionmaker(cls.engine, expire_on_commit=False)
        base_module.engine = cls.engine
        base_module.Session = cls.session_factory
        from src.routes import analysis, bills, records, reports
        for module in (analysis, bills, records, reports):
            module.Session = cls.session_factory
        Base.metadata.drop_all(cls.engine)
        Base.metadata.create_all(cls.engine)
        with cls.session_factory.begin() as db:
            observation = Observation(source_key="data:http-row", payload=ROW)
            db.add(observation)
            db.flush()
            run = AnalysisRun(title="HTTP 透视", owner="analyst@demo.local", parameters=PARAMETERS,
                              last_observation_id=observation.id, sample_size=1,
                              observation_ids=[observation.id], status="succeeded", result={})
            db.add(run)
            db.flush()
            cls.run_id = run.id
        cls.client = TestClient(app)
        cls.tokens = {}
        for role, email, password in (
            ("analyst", "analyst@demo.local", "Analyst-2026!"),
            ("supervisor", "supervisor@demo.local", "Supervisor-2026!"),
            ("viewer", "viewer@demo.local", "Viewer-2026!"),
        ):
            response = cls.client.post("/api/auth/login", json={"email": email, "password": password})
            assert response.status_code == 200, response.text
            cls.tokens[role] = response.json()["token"]

    def _auth(self, role):
        return {"Authorization": f"Bearer {self.tokens[role]}"}

    def _create_bill(self):
        response = self.client.post("/api/bills", json={"run_id": self.run_id, "title": "9月热水账单"},
                                    headers=self._auth("analyst"))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def _reading(self, bill_id, minute, reading):
        return self.client.post(f"/api/bills/{bill_id}/late-readings", json={
            "meter_code": "XQ-R01", "reading_date": "2026-09-01",
            "reading_minute": minute, "reading_m3": reading, "note": "晚到",
        }, headers=self._auth("analyst"))

    def test_full_lifecycle_and_acceptance_rules(self):
        # Viewer cannot create a bill.
        denied = self.client.post("/api/bills", json={"run_id": self.run_id}, headers=self._auth("viewer"))
        self.assertEqual(denied.status_code, 403)

        bill_id = self._create_bill()
        detail = self.client.get(f"/api/bills/{bill_id}", headers=self._auth("viewer")).json()
        self.assertEqual(detail["original_total_yuan"], 136.83)
        self.assertTrue(detail["reconciled"])
        self.assertEqual(detail["balance_due_yuan"], 0.0)

        # First late bottom: pending balance reflects the new reading immediately.
        response = self._reading(bill_id, 1080, 1220.000)
        self.assertEqual(response.status_code, 201, response.text)
        detail = self.client.get(f"/api/bills/{bill_id}", headers=self._auth("analyst")).json()
        self.assertEqual(detail["pending_adjustment_yuan"], -1.14)
        self.assertEqual(detail["balance_due_yuan"], -1.14)
        self.assertTrue(detail["reconciled"])
        first_adjustment = next(a for a in detail["adjustments"] if a["status"] == "pending")
        # Make-up already traces to a concrete meter bottom.
        self.assertEqual(first_adjustment["late_readings"][0]["meter_code"], "XQ-R01")

        # Duplicate bottom is rejected.
        self.assertEqual(self._reading(bill_id, 1080, 1221.0).status_code, 409)

        # Analyst hands the pending make-up to the clerk; amount freezes.
        submitted = self.client.post(f"/api/bills/{bill_id}/submit", json={}, headers=self._auth("analyst"))
        self.assertEqual(submitted.status_code, 201, submitted.text)
        adjustment_id = submitted.json()["id"]
        self.assertEqual(submitted.json()["amount_yuan"], -1.14)

        # A second late bottom while the first is with the clerk.
        response = self._reading(bill_id, 1320, 1225.000)
        self.assertEqual(response.status_code, 201, response.text)
        detail = self.client.get(f"/api/bills/{bill_id}", headers=self._auth("analyst")).json()
        frozen = next(a for a in detail["adjustments"] if a["id"] == adjustment_id)
        pending = next(a for a in detail["adjustments"] if a["status"] == "pending")
        # The clerk's copy still settles at the original frozen amount...
        self.assertEqual(frozen["amount_yuan"], -1.14)
        # ...while the pending balance reflects the newer reading.
        self.assertEqual(pending["amount_yuan"], -2.48)
        self.assertEqual(detail["balance_due_yuan"], round(-1.14 - 2.48, 2))
        self.assertTrue(detail["reconciled"])

        # Only the supervisor (clerk role) may settle.
        forbidden = self.client.post(f"/api/bills/adjustments/{adjustment_id}/settle",
                                     json={"idempotency_key": "x-0000000"}, headers=self._auth("analyst"))
        self.assertEqual(forbidden.status_code, 403)

        # A failed checkout posts nothing and leaves the adjustment submitted.
        failed = self.client.post(f"/api/bills/adjustments/{adjustment_id}/settle",
                                  json={"idempotency_key": "settle-fail-1", "force_failure": True},
                                  headers=self._auth("supervisor"))
        self.assertEqual(failed.status_code, 200, failed.text)
        self.assertEqual(failed.json()["outcome"], "failed")
        self.assertEqual(failed.json()["status"], "submitted")

        # Replaying the same key after the failure returns the recorded attempt, no double row.
        replay = self.client.post(f"/api/bills/adjustments/{adjustment_id}/settle",
                                  json={"idempotency_key": "settle-fail-1", "force_failure": False},
                                  headers=self._auth("supervisor"))
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(replay.json()["outcome"], "failed")

        # A fresh key completes settlement at the frozen amount.
        success = self.client.post(f"/api/bills/adjustments/{adjustment_id}/settle",
                                   json={"idempotency_key": "settle-ok-1", "force_failure": False},
                                   headers=self._auth("supervisor"))
        self.assertEqual(success.status_code, 200, success.text)
        self.assertEqual(success.json()["outcome"], "success")
        self.assertEqual(success.json()["amount_yuan"], -1.14)
        self.assertEqual(success.json()["status"], "settled")

        # Retrying the success key is idempotent and does not post again.
        replay_ok = self.client.post(f"/api/bills/adjustments/{adjustment_id}/settle",
                                     json={"idempotency_key": "settle-ok-1", "force_failure": False},
                                     headers=self._auth("supervisor"))
        self.assertTrue(replay_ok.json()["idempotent_replay"])

        # A brand-new key against an already-settled adjustment is refused.
        refused = self.client.post(f"/api/bills/adjustments/{adjustment_id}/settle",
                                   json={"idempotency_key": "settle-other-1", "force_failure": False},
                                   headers=self._auth("supervisor"))
        self.assertEqual(refused.status_code, 409)

        # Final reconciliation: original + settled + submitted + pending == latest recomputation.
        detail = self.client.get(f"/api/bills/{bill_id}", headers=self._auth("viewer")).json()
        self.assertTrue(detail["reconciled"])
        self.assertAlmostEqual(
            detail["original_total_yuan"] + detail["settled_adjustment_yuan"]
            + detail["submitted_adjustment_yuan"] + detail["pending_adjustment_yuan"],
            detail["latest_total_yuan"], places=6)
        # Exactly two settlement attempts were persisted (one fail, one success).
        self.assertEqual(len(detail["settlement_attempts"]), 2)
        # Every adjustment still traces to its source meter bottoms.
        for adjustment in detail["adjustments"]:
            self.assertTrue(adjustment["late_readings"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
