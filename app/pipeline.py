"""Orchestration: pre-flight data check -> Planner -> Executor <-> Evaluator (feedback loop) -> Planner finalises."""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from . import config, data_access, llm
from .agents import EvaluatorAgent, ExecutorAgent, PlannerAgent
from .analysis import Evidence
from .preflight import ensure_ready
from .engines.markovian import MarkovianEngine, RunHistory
from .errors import AppError, LLMError, RequestError
from .memory import InvestigationMemory
from .tracing import Tracer

MAX_REVISIONS = 2


def _validate(req) -> tuple[str, str, dict]:
    if not isinstance(req, dict):
        raise RequestError("Request body must be a JSON object.", stage="request")
    query = req.get("user_query")
    if not isinstance(query, str) or not query.strip():
        raise RequestError("'user_query' is required and must be a non-empty string.", stage="request")
    if len(query) > 2000:
        raise RequestError("'user_query' must be at most 2000 characters.", stage="request")
    request_id = req.get("request_id")
    request_id = request_id if isinstance(request_id, str) and request_id else f"req-{uuid.uuid4().hex[:8]}"
    context = req.get("context") if isinstance(req.get("context"), dict) else {}
    return request_id, query.strip(), context


def history_path() -> Path:
    base = Path(config.get("MEMORY_DIR", ".memory"))
    return (base if base.is_absolute() else config.ROOT / base) / "markovian_history.jsonl"


def run_request(req, tracer: Tracer | None = None, memory: InvestigationMemory | None = None) -> tuple[dict, int]:
    tracer = tracer or Tracer()
    request_id = req.get("request_id") if isinstance(req, dict) and isinstance(req.get("request_id"), str) else None
    started = time.time()
    try:
        request_id, user_query, context = _validate(req)
        memory = memory or InvestigationMemory()
        llm_mode = llm.mode()
        tracer.event("Orchestrator", "request_received", input_summary=user_query,
                     output_summary=f"data source: {data_access.source_name()}; reasoning: {llm_mode}")
        # GATE: no agent is created until Zetaris confirms the data is loaded (SELECT COUNT(1) on each table).
        preflight = ensure_ready(tracer)
        evidence, engine = Evidence(), MarkovianEngine()
        planner = PlannerAgent(tracer, memory)
        executor = ExecutorAgent(tracer, engine, evidence, preflight["degraded"])
        evaluator = EvaluatorAgent(tracer)

        plan = planner.plan(user_query, context, evidence)
        tracer.event("PlannerAgent", "handoff", target_agent="ExecutorAgent",
                     input_summary="retrieval + investigation plan", output_summary=f"hypotheses: "
                     f"{[h['dimension'] for h in plan['hypotheses']]}")

        state = executor.investigate(user_query, plan)
        draft = executor.draft(user_query, state)

        revision_count = 0
        critiques = 0
        evaluator_findings: list[str] = []
        model_notes: list[str] = []
        review = evaluator.review(user_query, state, draft, evidence, executor)
        while review["requires_revision"] and revision_count < MAX_REVISIONS:
            critiques += 1
            evaluator_findings += [i["message"] for i in review["issues"]]
            try:
                draft = executor.revise(user_query, state, draft, review)
            except LLMError as exc:    # keep the last reviewed draft and say so; do not lose the whole run
                model_notes.append("A revision call to the model failed (" + str(exc)[:80] + "); the draft shown is the last one "
                                   "the Evaluator reviewed, and its open issues are listed.")
                tracer.event("ExecutorAgent", "revision_unavailable", status="degraded", output_summary=str(exc)[:160])
                break
            revision_count += 1
            review = evaluator.review(user_query, state, draft, evidence, executor)
        if review.get("critic_unavailable"):
            model_notes.append("The model critic was unavailable for the last review; only the deterministic checks ran.")
        unresolved = review["requires_revision"]
        if unresolved:
            critiques += 1

        result = planner.finalise(plan, state, draft, review, evidence, executor, revision_count, unresolved)

        result["caveats"] = list(result["caveats"]) + model_notes
        duration_ms = int((time.time() - started) * 1000)
        run = engine.run_record(user_query, engine.status_for(state.stopped), duration_ms)
        history = RunHistory(history_path())
        history.record(run)
        response = {
            "request_id": request_id,
            "trace_id": tracer.trace_id,
            "status": "success",
            "preflight": {"ready": True, "checks": preflight["checks"], "degraded": preflight["degraded"]},
            "agents_used": [a for a in tracer.agents_seen() if a.endswith("Agent")],
            "result": result,
            "collaboration": {
                # True only when the Evaluator's last review approved the draft; unresolved issues are listed.
                "feedback_loop_completed": not unresolved,
                "evaluator_reviews": revision_count + 1,
                "critiques_issued": critiques,
                "revision_count": revision_count,
                "evaluator_findings": evaluator_findings,
                "unresolved_issues": [i["message"] for i in review["issues"]] if unresolved else [],
                "final_checks": review["checks"],
            },
            "engines": {
                "markovian": engine.stats() | {"run": {k: run[k] for k in (
                    "id", "status", "totalTokensUsed", "totalTokensSaved", "efficiencyPercent", "durationMs")},
                    "cumulative": history.cumulative()},
                "h_mem": {"recalled": len(plan.get("memory_recalled", [])),
                          "feedback": result["memory"]["feedback_applied"], "stored": len(result["memory"]["stored"])},
            },
            "models_used": tracer.models_used,
            "reasoning_mode": llm_mode,
            "data_source": data_access.source_name(),
            "log_path": _log_path(tracer),
        }
        return response, 200
    except AppError as exc:
        tracer.event("Orchestrator", "request_failed", status="error", input_summary=str(req)[:200],
                     output_summary=f"{exc.kind}: {exc.message}", stage=exc.stage)
        return _error(request_id, tracer, exc.to_dict()), exc.http_status
    except Exception as exc:  # unexpected: log detail, return a structured generic error
        tracer.event("Orchestrator", "request_failed", status="error", input_summary=str(req)[:200],
                     output_summary=f"{type(exc).__name__}: {exc}")
        return _error(request_id, tracer, {"type": "internal_error", "message": "Unexpected internal error; see trace.",
                                           "stage": "unknown", "details": {}}), 500


def _log_path(tracer: Tracer) -> str:
    try:
        return str(tracer.path.relative_to(config.ROOT))
    except ValueError:
        return str(tracer.path)


def _error(request_id, tracer: Tracer, error: dict) -> dict:
    return {"request_id": request_id, "trace_id": tracer.trace_id, "status": "error", "error": error,
            "agents_used": [a for a in tracer.agents_seen() if a.endswith("Agent")],
            "log_path": _log_path(tracer)}
