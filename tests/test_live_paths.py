"""Live-mode code paths against local stand-ins (tests/helpers.py): HTTP contract, headers, the pre-flight gate, model
logging, truncation. They do NOT prove real Zetaris or Nightshift behave this way (see README 'Verified vs unverified')."""
import unittest

from app import data_access, llm, pipeline
from app.errors import DataError
from app.memory import InvestigationMemory
from tests import helpers as h


def live_env(z, n, d, **extra):
    cfg = dict(ZETARIS_REST_URL=z.url, ZETARIS_API_KEY="z-key", ZETARIS_ORG_ID="org-1",
               METERLESS_BASE_URL=n.url if n else None, METERLESS_RUNTIME_TOKEN="test-runtime-token" if n else None,
               MEMORY_DIR=str(d / "m"), LOG_DIR=str(d / "l"), LLM_BACKOFF_SECONDS="0", ZETARIS_BACKOFF_SECONDS="0")
    cfg.update(extra)
    return h.env(**cfg)


class TestLive(unittest.TestCase):
    def test_zetaris_request_shape(self):
        with h.tempdir() as d, h.FakeZetaris() as z, live_env(z, None, d):
            r = data_access.query("SELECT COUNT(1) AS n FROM fixfirst.work_orders", ["n"])
        self.assertEqual(r.source, "zetaris")
        req = z.requests[0]
        self.assertEqual(req["path"], "/api/v1.0/sql-editor/sqls/run")
        self.assertEqual(req["headers"]["authorization"], "Bearer z-key")
        self.assertEqual(req["headers"]["x-org-id"], "org-1")
        self.assertEqual(set(req["body"]), {"queryId", "sql", "source", "limit"})

    def test_full_live_run_counts_first_and_logs_the_model(self):
        with h.tempdir() as d, h.FakeZetaris(shape="lists") as z, h.FakeNightshift() as n, live_env(z, n, d):
            resp, status = pipeline.run_request({"user_query": "What should the Ward 7 crews take first?"},
                                                memory=InvestigationMemory())
        self.assertEqual(status, 200, resp.get("error"))
        self.assertEqual(resp["data_source"], "zetaris")
        self.assertEqual(resp["reasoning_mode"], "nightshift")
        self.assertEqual(resp["models_used"], ["nvidia/test-model-1"])
        sqls = [q["body"]["sql"] for q in z.requests]
        self.assertTrue(sqls[0].startswith("SELECT COUNT(1)"))            # pre-flight comes before anything else
        self.assertTrue(any("JOIN" in s and "sla_policy" in s for s in sqls))   # work orders joined to the policy table
        self.assertTrue(any("record_set = 'history_sample'" in s and "closed_date" in s for s in sqls))   # history baseline
        self.assertTrue(any("us_holidays" in s for s in sqls) and any("weather_daily" in s for s in sqls))
        first_model_call = n.requests[0]
        self.assertEqual(first_model_call["headers"]["authorization"], "Bearer test-runtime-token")
        self.assertEqual(first_model_call["headers"]["meterless-app"], "fix-first")
        self.assertEqual(first_model_call["headers"]["meterless-scope"], "user")
        self.assertEqual(first_model_call["body"]["model"], "meterless/auto")
        self.assertNotIn("SELECT", " ".join(m["content"] for m in first_model_call["body"]["messages"] if m["role"] == "system"))

    def test_data_not_loaded_means_no_model_call_and_no_agent(self):
        with h.tempdir() as d, h.FakeZetaris(fail_status=500, fail_count=99) as z, h.FakeNightshift() as n, live_env(z, n, d):
            resp, status = pipeline.run_request({"user_query": "What should crews fix first?"}, memory=InvestigationMemory())
        self.assertEqual(status, 503)
        self.assertEqual(resp["error"]["type"], "data_not_ready")
        self.assertEqual(n.requests, [])
        self.assertEqual(resp["agents_used"], [])

    def test_truncated_work_set_is_caught_by_the_evaluator(self):
        with h.tempdir() as d, h.FakeZetaris(row_cap=100) as z, h.FakeNightshift() as n, live_env(z, n, d):
            resp, status = pipeline.run_request({"user_query": "What should crews fix first across the city?"},
                                                memory=InvestigationMemory())
        # the row cap also hits the pre-flight COUNT? no: COUNT returns one row. The 810-order work set is cut to 100.
        self.assertEqual(status, 200)
        self.assertFalse(resp["collaboration"]["feedback_loop_completed"])
        self.assertTrue(any("Zetaris counts" in m for m in resp["collaboration"]["unresolved_issues"]))

    def test_nightshift_retry_on_503(self):
        with h.tempdir() as d, h.FakeNightshift(fail_first=1) as n, h.env(METERLESS_BASE_URL=n.url, METERLESS_RUNTIME_TOKEN="test-runtime-token",
                                                                       LLM_BACKOFF_SECONDS="0", LOG_DIR=str(d)):
            out = llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(out["retries"], 1)


if __name__ == "__main__":
    unittest.main()
