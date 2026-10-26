"""End-to-end in SAMPLE_MODE on the REAL work-order extract. Ground truth is computed with the csv module only."""
import json
import unittest
from collections import Counter

from app import agents, pipeline
from app.memory import InvestigationMemory
from tests import helpers as h

ROOT = h.ROOT


def run(query, d, **extra):
    with h.env(SAMPLE_MODE="true", MEMORY_DIR=str(d / "m"), LOG_DIR=str(d / "l"), **extra):
        return pipeline.run_request({"user_query": query}, memory=InvestigationMemory())


class TestPipelineSample(unittest.TestCase):
    def test_citywide_numbers_match_independent_csv_counts(self):
        rows = h.open_rows()
        with h.tempdir() as d:
            resp, status = run("Which open work orders should crews fix first across the city?", d)
        self.assertEqual(status, 200, resp.get("error"))
        r = resp["result"]
        self.assertEqual(r["totals"]["orders"], len(rows))
        by_group = Counter(x["asset_group"] for x in rows)
        for f in r["findings"]:
            if f["dimension"] == "asset_group" and f["level"] == 0:
                self.assertEqual(f["orders"], by_group[f["member"]])
        unp = sum(1 for x in rows if x["priority"] == "")
        self.assertEqual(r["data_quality"]["unprioritised_orders"], unp)
        self.assertEqual(resp["models_used"], ["offline-rules"])
        self.assertEqual(resp["data_source"], "sample_data")
        self.assertTrue(resp["preflight"]["ready"])
        self.assertTrue(resp["collaboration"]["feedback_loop_completed"])
        self.assertTrue(all(v is not False for v in resp["collaboration"]["final_checks"].values()))
        self.assertTrue(any("SYNTHETIC" in c for c in r["caveats"]))     # sample weather must be labelled

    def test_ward_scope_and_priority_list_stay_inside_scope(self):
        with h.tempdir() as d:
            resp, status = run("What should the Ward 7 crews take first?", d)
        self.assertEqual(status, 200)
        r = resp["result"]
        self.assertEqual(r["scope"], "ward=7")
        self.assertEqual(r["totals"]["orders"], sum(1 for x in h.open_rows() if x["ward"] == "7"))
        self.assertTrue(all(o["ward"] == "7" for o in r["priority_list"]))
        scores = [o["score"] for o in r["priority_list"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_offline_planner_ignores_a_ward_that_is_not_in_the_profile(self):
        with h.tempdir() as d, h.env(SAMPLE_MODE="true", MEMORY_DIR=str(d / "m"), LOG_DIR=str(d / "l")):
            from app import offline
            self.assertEqual(offline.respond([{"role": "system", "content": "ROLE: planner"},
                                              {"role": "user", "content": "QUESTION: ward 9\nPROFILE_JSON (open orders per member):\n{}"}])["scope"], [])

    def test_unsupported_empty_question_rejected(self):
        with h.tempdir() as d, h.env(SAMPLE_MODE="true", MEMORY_DIR=str(d / "m"), LOG_DIR=str(d / "l")):
            resp, status = pipeline.run_request({"user_query": "  "}, memory=InvestigationMemory())
        self.assertEqual(status, 422)

    def test_evaluator_sends_back_an_ungrounded_draft_and_executor_revises(self):
        original = agents.ExecutorAgent._write
        calls = {"n": 0}

        def bad_first(self, q, state, version, extra=""):
            draft = original(self, q, state, version, extra)
            calls["n"] += 1
            if calls["n"] == 1:
                draft["summary"] += " Crews will clear 4,321 orders by Friday."
            return draft
        with h.tempdir() as d:
            agents.ExecutorAgent._write = bad_first
            try:
                resp, status = run("Which open work orders should crews fix first across the city?", d)
            finally:
                agents.ExecutorAgent._write = original
        self.assertEqual(status, 200)
        c = resp["collaboration"]
        self.assertEqual(c["revision_count"], 1)
        self.assertTrue(any("4,321" in m or "4321" in m for m in c["evaluator_findings"]))
        self.assertTrue(c["feedback_loop_completed"])

    def test_evaluator_catches_tampered_hotspot_numbers(self):
        original = agents.ExecutorAgent.enrich

        def tamper(self, state):
            original(self, state)
            if state.findings:
                state.findings[0]["orders"] += 7
        with h.tempdir() as d:
            agents.ExecutorAgent.enrich = tamper
            try:
                resp, status = run("Which open work orders should crews fix first across the city?", d)
            finally:
                agents.ExecutorAgent.enrich = original
        c = resp["collaboration"]
        self.assertFalse(c["feedback_loop_completed"])
        self.assertTrue(any("Hot spot" in m for m in c["unresolved_issues"]))

    def test_history_baseline_is_used_and_checked(self):
        original = agents.ExecutorAgent._load_history

        def tamper(self):
            original(self)
            self.history["closed_in_sample"] += 1          # a statistic the data does not support
        with h.tempdir() as d:
            resp, _ = run("Which open work orders should crews fix first across the city?", d)
            self.assertEqual(resp["result"]["history_baseline"]["closed_in_sample"],
                             sum(1 for x in h.load_rows() if x["record_set"] == "history_sample" and x["closed_date"]))
            self.assertTrue(resp["collaboration"]["final_checks"]["history_reread"])
            self.assertTrue(any(c["finding_id"] == "hist" for c in resp["result"]["claims"]))
            agents.ExecutorAgent._load_history = tamper
            try:
                resp2, _ = run("Which open work orders should crews fix first across the city?", d)
            finally:
                agents.ExecutorAgent._load_history = original
        self.assertFalse(resp2["collaboration"]["feedback_loop_completed"])
        self.assertTrue(any("historical baseline" in m for m in resp2["collaboration"]["unresolved_issues"]))

    def test_history_rows_never_enter_the_open_backlog(self):
        with h.tempdir() as d:
            resp, _ = run("Which open work orders should crews fix first across the city?", d)
        self.assertEqual(resp["result"]["totals"]["orders"], len([x for x in h.open_rows() if x["record_set"] == "active_fy2026"]))

    def test_markovian_prompt_size_stays_flat_and_memory_is_written(self):
        with h.tempdir() as d:
            resp, _ = run("Which open work orders should crews fix first across the city?", d)
            m = resp["engines"]["markovian"]
            self.assertLessEqual(max(m["prompt_chars_per_step"]), 4000)
            self.assertGreaterEqual(m["steps"], 3)
            self.assertGreater(resp["engines"]["h_mem"]["stored"], 0)
            lp = resp["log_path"]
            with open(lp if lp.startswith("/") else ROOT / lp) as fh:
                log = [json.loads(x) for x in fh]
        actions = {e["action"] for e in log}
        self.assertTrue({"preflight_data_check", "create_plan", "markov_transition", "approve_draft", "memory_store"} <= actions)
        self.assertEqual(log[1]["action"], "preflight_data_check")        # the gate runs before any agent acts

    def test_every_example_input_runs(self):
        for p in sorted((ROOT / "input_examples").glob("*.json")):
            with h.tempdir() as d:
                resp, status = run(json.loads(p.read_text())["user_query"], d)
            self.assertEqual(status, 200, f"{p.name}: {resp.get('error')}")
            self.assertGreaterEqual(len(resp["result"]["claims"]), 2)


if __name__ == "__main__":
    unittest.main()
