"""Regressions from the first live run (trace-20261026-c750c594)."""
import unittest

from app import analysis, agents


class LiveFindings(unittest.TestCase):
    def test_ordinal_is_not_a_claim(self):
        self.assertEqual(analysis.ungrounded_numbers("the 75th percentile and 2nd quartile", {"a": 1.0}), [])

    def test_real_ungrounded_number_still_caught(self):
        self.assertEqual(analysis.ungrounded_numbers("it took 75 days", {"a": 1.0}), ["75 days"[:2]])

    def test_lookup_tables_not_capped_at_default(self):
        self.assertGreaterEqual(agents.LOOKUP_LIMIT, 1349 * 3)  # weather rows in the live table, with headroom

    def test_brief_reports_days_over_target(self):
        t = {"work_order_id": 1, "description": "x", "priority_level": 5, "age_working_days": 8, "target_days": 1, "overdue": True}
        self.assertEqual(agents._brief(t)["working_days_over_target"], 7)

    def test_domain_regex_catches_questions_the_model_refused(self):
        for q in ("What should the Ward 7 crews take first, and which of those orders are overdue?",
                  "Give me the top 10 transportation (TOA) work orders to dispatch first, and tell me whether weather explains any of the delay."):
            self.assertTrue(agents._DOMAIN.search(q), q)
        self.assertFalse(agents._DOMAIN.search("What is the capital of France?"))

    def test_first_token_504_falls_back_to_fast_mode(self):
        import os
        from unittest import mock
        from app import llm

        calls = []

        class R:
            def __init__(self, code, body):
                self.status_code, self.text, self._b = code, body, body

            def json(self):
                import json
                return json.loads(self._b)

        def post(url, headers=None, json=None, timeout=None):
            calls.append((json["model"], headers.get("meterless-ttfb-ms")))
            if len(calls) == 1:
                return R(504, '{"error":{"code":"first_token_timeout"}}')
            return R(200, '{"model":"x/y","choices":[{"message":{"content":"ok"}}]}')

        env = {"METERLESS_BASE_URL": "http://h/v1", "METERLESS_RUNTIME_TOKEN": "t", "LLM_BACKOFF_SECONDS": "0"}
        with mock.patch.dict(os.environ, env), mock.patch("requests.post", post):
            out = llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(calls[0][0], "meterless/auto")
        self.assertEqual(calls[1][0], "meterless/fast")
        self.assertEqual(calls[0][1], "300000")
        self.assertEqual(out["content"], "ok")

    def test_priority_label_must_match_the_hot_spot(self):
        from app.agents import EvaluatorAgent
        payload = {"findings": [{"id": "f1", "dimension": "asset_group", "member": "UFA", "scope": "ward=7",
                                 "top_orders": [{"priority_level": 5}]}]}
        bad, good = [], []
        self.assertFalse(EvaluatorAgent._check_labels(payload, {"claims": [{"finding_id": "f1", "statement": "priority level 0, UFA"}]}, bad))
        self.assertTrue(EvaluatorAgent._check_labels(payload, {"claims": [{"finding_id": "f1", "statement": "priority level 5 orders"}]}, good))
        self.assertEqual(bad[0]["code"], "label_mismatch")


if __name__ == "__main__":
    unittest.main()
