import json
import unittest

import run
from tests import helpers as h


class TestApi(unittest.TestCase):
    def _env(self, d):
        return h.env(SAMPLE_MODE="true", MEMORY_DIR=str(d / "m"), LOG_DIR=str(d / "l"))

    def test_health_and_data_health(self):
        with h.tempdir() as d, self._env(d):
            self.assertEqual(run.handle("GET", "/health", b"")[0], 200)
            status, body = run.handle("GET", "/health/data", b"")
        self.assertEqual(status, 200)
        self.assertTrue(body["ready"])

    def test_run_validates_body(self):
        with h.tempdir() as d, self._env(d):
            self.assertEqual(run.handle("POST", "/run", b"not json")[0], 400)
            self.assertEqual(run.handle("POST", "/run", b"{}")[0], 422)
            self.assertEqual(run.handle("GET", "/nope", b"")[0], 404)

    def test_run_returns_the_contract(self):
        body = json.dumps({"request_id": "t1", "user_query": "What should crews fix first?"}).encode()
        with h.tempdir() as d, self._env(d):
            status, out = run.handle("POST", "/run", body)
        self.assertEqual(status, 200)
        for key in ("request_id", "trace_id", "status", "agents_used", "result", "collaboration", "engines",
                    "models_used", "data_source", "log_path", "preflight"):
            self.assertIn(key, out)
        self.assertEqual(set(out["agents_used"]), {"PlannerAgent", "ExecutorAgent", "EvaluatorAgent"})


if __name__ == "__main__":
    unittest.main()
