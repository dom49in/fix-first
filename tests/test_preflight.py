"""The pre-flight gate: agents must not run until Zetaris confirms the data is loaded."""
import unittest
from unittest import mock

from app import data_access, pipeline, preflight
from app.errors import DataError
from app.memory import InvestigationMemory
from tests import helpers as h


def sample_env(d):
    return h.env(SAMPLE_MODE="true", MEMORY_DIR=str(d / "m"), LOG_DIR=str(d / "l"))


class TestPreflight(unittest.TestCase):
    def test_ready_when_all_tables_have_rows_and_manifest_matches(self):
        with h.tempdir() as d, sample_env(d):
            r = preflight.run_preflight()
        self.assertTrue(r["ready"])
        by = {c["role"]: c for c in r["checks"]}
        self.assertEqual(by["work_orders"]["rows"], len(h.load_rows()))
        self.assertEqual(set(by), {"work_orders", "sla_policy", "history", "holidays", "weather"})
        self.assertEqual(r["degraded"], [])

    def test_empty_or_unreachable_work_orders_block(self):
        real = data_access.query

        def fake(sql, cols, limit=None):
            if "work_orders" in sql and "COUNT(1)" in sql:
                raise DataError("table not found", stage="zetaris")
            return real(sql, cols, limit)
        with h.tempdir() as d, sample_env(d), mock.patch.object(data_access, "query", fake):
            r = preflight.run_preflight()
        self.assertFalse(r["ready"])
        self.assertEqual(r["blocking"], ["work_orders"])

    def test_partial_load_is_detected_from_the_manifest(self):
        real = data_access.query

        class R:
            rows = [{"n": 250}]
        def fake(sql, cols, limit=None):
            return R() if "work_orders" in sql and "COUNT(1)" in sql else real(sql, cols, limit)
        with h.tempdir() as d, sample_env(d), mock.patch.object(data_access, "query", fake):
            r = preflight.run_preflight()
        c = next(c for c in r["checks"] if c["role"] == "work_orders")
        self.assertEqual(c["status"], "partial_load")
        self.assertFalse(r["ready"])

    def test_missing_web_api_table_degrades_but_does_not_block(self):
        real = data_access.query

        def fake(sql, cols, limit=None):
            if "weather_daily" in sql and "COUNT(1)" in sql:
                raise DataError("no such table", stage="zetaris")
            return real(sql, cols, limit)
        with h.tempdir() as d, sample_env(d), mock.patch.object(data_access, "query", fake):
            r = preflight.run_preflight()
        self.assertTrue(r["ready"])
        self.assertEqual(r["degraded"], ["weather"])

    def test_require_all_makes_optional_tables_blocking(self):
        real = data_access.query

        def fake(sql, cols, limit=None):
            if "us_holidays" in sql and "COUNT(1)" in sql:
                raise DataError("no such table", stage="zetaris")
            return real(sql, cols, limit)
        with h.tempdir() as d, h.env(SAMPLE_MODE="true", PREFLIGHT_REQUIRE_ALL="true", MEMORY_DIR=str(d / "m"),
                                     LOG_DIR=str(d / "l")), mock.patch.object(data_access, "query", fake):
            self.assertFalse(preflight.run_preflight()["ready"])

    def test_pipeline_refuses_to_start_agents_when_not_ready(self):
        real = data_access.query

        def fake(sql, cols, limit=None):
            if "COUNT(1)" in sql and "work_orders" in sql:
                raise DataError("down", stage="zetaris")
            return real(sql, cols, limit)
        with h.tempdir() as d, sample_env(d), mock.patch.object(data_access, "query", fake):
            resp, status = pipeline.run_request({"user_query": "What should crews fix first?"},
                                                memory=InvestigationMemory())
        self.assertEqual(status, 503)
        self.assertEqual(resp["error"]["type"], "data_not_ready")
        self.assertEqual(resp["agents_used"], [])          # no agent ever started
        self.assertIn("load_to_zetaris", resp["error"]["message"])


if __name__ == "__main__":
    unittest.main()
