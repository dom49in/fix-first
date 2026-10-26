"""Planner, Executor and Evaluator agents and the collaboration between them (Track 3: Reasoning Architecture).

Planner   : turns the question into a plan (optional base scope, ordered hypotheses, list size) from a live profile of the
            open backlog, recalls H-MEM memories as hints, and finalises (scores recalled memories against today's data,
            mines new ones).
Executor  : pulls the open work orders from Zetaris joined to the SLA policy, loads the calendar (public holidays) and
            weather from their Zetaris REST tables, scores every order, then runs the Markovian triage loop (the model
            picks which dimension to probe next; each probe is a Zetaris GROUP BY). It drafts the answer and revises it
            when the Evaluator pushes back.
Evaluator : recomputes every number a second way (Python over the work set versus Zetaris aggregates, plus fresh
            re-queries), checks the ranking, the calendar inputs, grounding of every number in the text, disclosure of
            data-quality gaps, then asks a model to critique unsupported statements. It can send work back.
"""
from __future__ import annotations

import json
import re
from datetime import date

from . import analysis, config, data_access, llm, scoring
from . import schema as S
from .analysis import Evidence
from .engines.markovian import MarkovianEngine, MarkovState, scope_key
from .errors import AppError, LLMError, NoDataError, RequestError
from .memory import InvestigationMemory
from .tracing import Tracer, short

MIN_COVERAGE = 0.50
PLAN_SYSTEM = (
    "ROLE: planner\n"
    "You plan a triage of OPEN maintenance work orders (city asset maintenance) using only the provided profile.\n"
    "Reply with ONLY a JSON object:\n"
    '{"scope": [{"dimension": "<dimension>", "member": "<member exactly as listed in PROFILE_JSON>"}], '
    '"hypotheses": [{"dimension": "<one of the dimensions>", "rationale": "<max 15 words>"}], '
    '"top_n": <3-10>, "success_criteria": "<one sentence>"}\n'
    "- scope: filters that the QUESTION itself asks for (a ward, an asset group...). At most 2. Empty list for a "
    "city-wide question. Members must be copied from PROFILE_JSON.\n"
    "- hypotheses: dimensions ordered from most to least likely to concentrate the weighted backlog. PAST_MEMORY_JSON lists "
    "earlier hot spots and ruled-out dimensions: test confirmed ones first, ruled-out ones last; they are hints only and "
    "will be re-checked against the data. Never list a dimension already fixed by scope.\n"
    "- priority_level 0 means the work order has NO priority recorded (a data-quality gap). For a question about orders with no priority, set scope priority_level=0.\n"
    "- The Executor computes age, overdue status, weather allowance and SLA targets from the work orders, so questions about "
    "overdue orders, the oldest orders, the top N, weather or SLA ARE answerable even though PROFILE_JSON only shows counts.\n"
    '- Reply {"unsupported": true, "reason": "<why>"} ONLY if the question is not about open maintenance work orders '
    "(for example another city, closed-order statistics, or an unrelated topic)."
)
_META = re.compile(r"json object|required keys|numbered plan|previous (output|reply|response)", re.I)
_DOMAIN = re.compile(r"work order|workorder|crew|dispatch|overdue|priorit|ward|backlog|triage|fix first|sla|weather|"
                     r"open order|oldest|tree|toa|ufa|street ?light", re.I)

DRAFT_SYSTEM = (
    "ROLE: executor-drafter\n"
    "You tell a maintenance manager WHERE to send crews first and WHY, using only EVIDENCE_JSON.\n"
    'Reply with ONLY a JSON object: {"summary": "<2-4 sentences>", '
    '"claims": [{"finding_id": "<id>", "statement": "<one or two sentences>"}]}\n'
    "Rules:\n"
    "- city_priority_list is the ranked answer to \"what first\". The summary MUST begin with its first order (work order id, "
    "description, priority_level, age_working_days against target_days) and add one claim with finding_id \"rank\" covering "
    "city_priority_list. Hot spots explain where the backlog sits; they do not outrank the list.\n"
    "- Describe where the weighted backlog sits ONLY with the findings: use their dimension, member and share_of_total_backlog. "
    "Priority-5 orders are the urgent list (city_priority_list), not where the backlog concentrates; never say they hold the "
    "majority of points. Do not say a segment holds the majority unless its share_of_total_backlog is above 0.5.\n"
    "- age_working_days is how long an order has been open. Say it is overdue by working_days_over_target days, never that "
    "its age is the days overdue.\n"
    "- Every entry in findings needs at least one claim citing its id: say where it is, how many open orders and how many "
    "are past the SLA target (overdue_orders), and the recommended dispatch (which orders first, using top_orders).\n"
    "- overdue_net_of_weather_orders is the overdue count after giving back weather-impacted working days. If it is lower "
    "than overdue_orders, say weather explains part of the delay.\n"
    "- data_quality: if unprioritised_orders is not 0 add one claim with finding_id \"dq\" saying how many open orders have "
    "no priority and that they need triage; never guess their priority.\n"
    "- calendar: add one claim with finding_id \"cal\" on public holidays and weather days counted in the ages, using the "
    "numbers given.\n"
    "- policy.targets are SLA targets in working days. Those with source \"history\" were derived from the closed historical "
    "sample (75th percentile of closures within the cap); source \"assumption\" ones are assumptions; weights are always an "
    "assumption. Say so once when you mention targets.\n"
    "- history: add one claim with finding_id \"hist\" giving the typical time to close (median, working days) for the lead "
    "hot spot's asset group from history.by_asset_group, with closed_in_sample. If history.weather_effect.enough_data is false "
    "say the sample is too small to tell whether weather slows closures; never claim weather causes delay.\n"
    "- Do not say one finding's orders should be dispatched \"after\" or \"before\" another's: city_priority_list sets the order.\n"
    "- If asked whether weather explains delay, answer only from overdue_orders vs overdue_net_of_weather_orders, "
    "weather_impacted_working_days and history.weather_effect, and say the data cannot establish a cause.\n"
    "- Name a hot spot by its own dimension and member and scope. Do not attach a priority level to a hot spot that is not "
    "priority_level.\n"
    "- Never invent causes (crew shortages, contracts, budgets). You may say what the data cannot tell. Do not use any "
    "number that is not in the evidence (you may round it). Do not quote street addresses.\n"
    "- Never mention or guess staff names."
)

CRITIC_SYSTEM = (
    "ROLE: evaluator-critic\n"
    "You review a draft maintenance triage against EVIDENCE_JSON. Flag only MATERIAL problems: numbers that contradict the "
    "evidence, causes asserted that the data cannot support, recommendations not backed by a cited finding, statements about where the backlog concentrates that contradict the findings' share_of_total_backlog, or a "
    "data-quality gap that was left out. Dispatch orders that follow city_priority_list or a finding's top_orders are supported; flag ordering only if it contradicts the scores. The data-quality gap has its own claim (finding_id dq); do not require every claim to repeat it. Do not nitpick style.\n"
    'Reply with ONLY a JSON object: {"requires_revision": true|false, "issues": ["<specific issue>", ...]}.'
)


def _brief(t: dict) -> dict:
    out = {k: t[k] for k in ("work_order_id", "description", "priority_level", "age_working_days", "target_days", "overdue")}
    out["working_days_over_target"] = max(0, int(t["age_working_days"]) - int(t["target_days"]))
    return out


LOOKUP_LIMIT = 5000   # holidays and weather are small, bounded lookup tables: never let the default row cap cut them


def scope_text(scope: list[dict]) -> str:
    return " AND ".join(f"{s['dimension']}={s['member']}" for s in scope)


def dim_label(order: dict, dim: str) -> str:
    value = order.get(dim)
    if value in (None, ""):
        return "(none)"
    return str(int(value)) if dim == "priority_level" else str(value)


def group(scored: list[dict], dim: str, filters: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for o in scored:
        if scoring.matches(o, filters):
            g = out.setdefault(dim_label(o, dim), {"n_orders": 0, "points": 0.0})
            g["n_orders"] += 1
            g["points"] += o["weight"]
    return out


class PlannerAgent:
    name = "PlannerAgent"

    def __init__(self, tracer: Tracer, memory: InvestigationMemory):
        self.tracer, self.memory = tracer, memory

    def plan(self, user_query: str, context: dict, evidence: Evidence) -> dict:
        res = data_access.query(S.as_of_sql(), ["as_of", "first_day", "n"])
        evidence.add(S.as_of_sql(), res.rows, res.source, kind="range", truncated=res.truncated)
        if not res.rows or not res.rows[0].get("as_of"):
            raise NoDataError("The work-order table returned no rows.", stage="plan")
        as_of, first_day = scoring.parse_date(res.rows[0]["as_of"]), scoring.parse_date(res.rows[0]["first_day"])
        profile: dict[str, dict] = {}
        for dim in S.FILTER_DIMENSIONS:
            sql = S.breakdown_sql(dim, [])
            r = data_access.query(sql, S.BREAKDOWN_COLUMNS)
            evidence.add(sql, r.rows, r.source, kind="profile", dimension=dim, truncated=r.truncated)
            profile[dim] = {analysis.label(x["member"]): int(analysis.num(x["n_orders"])) for x in r.rows}
        if not any(profile["status"].values()):
            raise NoDataError("There are no open work orders to triage.", stage="plan")
        self.tracer.event(self.name, "query_data", input_summary="profile of open work orders", source=res.source,
                          output_summary=f"data as of {as_of}; {sum(profile['status'].values())} open orders; "
                                         + ", ".join(f"{d}:{len(m)}" for d, m in profile.items()))
        recall = self.memory.recall(user_query)
        hints = self.memory.hints(recall)
        self.tracer.event(self.name, "memory_recall", engine="H-MEM", input_summary=user_query,
                          output_summary=f"{len(hints)} recalled (hints only): {recall['trace']['retrievalReason']}",
                          memory_ids=[h["id"] for h in hints], scores=[h["score"] for h in hints])
        shown = {d: dict(sorted(m.items(), key=lambda kv: -kv[1])[:12]) for d, m in profile.items()}
        user = (f"QUESTION: {user_query}\nDIMENSIONS: {json.dumps(S.DIMENSIONS)}\n"
                f"PROFILE_JSON (open orders per member):\n{json.dumps(shown)}\n"
                f"DATA_AS_OF: {as_of}\nPAST_MEMORY_JSON:\n{json.dumps(hints)}")
        obj, meta = llm.chat_json([{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": user}])
        if obj.get("unsupported") and _DOMAIN.search(user_query):
            # The model refused a question that is plainly about open work orders. Retry once with the reason stated,
            # then fall back to rule-based parsing of the question (logged, never silent).
            retry_user = user + "\nNOTE: This question IS answerable; the Executor computes overdue status and weather. Plan it."
            obj, meta = llm.chat_json([{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": retry_user}])
            if obj.get("unsupported"):
                from . import offline
                obj = offline._plan(user)
                self.tracer.event(self.name, "plan_fallback", status="degraded", input_summary=user_query,
                                  output_summary="model declined an in-domain question twice; scope parsed by rules")
        if obj.get("unsupported"):
            raise RequestError(f"This question can't be answered from open work orders: {obj.get('reason', '')}",
                               stage="plan", details={"dimensions": S.DIMENSIONS})
        scope = []
        for f in (obj.get("scope") or [])[:S.MAX_BASE_FILTERS]:
            if not isinstance(f, dict) or f.get("dimension") not in S.FILTER_DIMENSIONS:
                continue
            dim, member = f["dimension"], analysis.label(f.get("member"))
            lookup = {m.lower(): m for m in profile[dim]}
            if member.lower() not in lookup:
                raise NoDataError(f"No open work orders with {dim}={member}. Known {dim} values: "
                                  f"{sorted(profile[dim])[:15]}.", stage="plan", details={"dimension": dim})
            scope.append({"dimension": dim, "member": lookup[member.lower()]})
        fixed = {f["dimension"] for f in scope}
        hyps = [{"dimension": h["dimension"], "rationale": str(h.get("rationale", ""))[:120]}
                for h in (obj.get("hypotheses") or [])
                if isinstance(h, dict) and h.get("dimension") in S.DIMENSIONS and h["dimension"] not in fixed]
        try:
            top_n = min(max(int(obj.get("top_n", 5)), 3), 10)
        except (TypeError, ValueError):
            top_n = 5
        plan = {"scope": scope, "hypotheses": hyps, "top_n": top_n, "as_of": as_of.isoformat(),
                "first_day": first_day.isoformat() if first_day else None, "profile": profile,
                "success_criteria": str(obj.get("success_criteria", ""))[:300],
                "memory_recalled": recall["memories"], "memory_trace": recall["trace"]}
        self.tracer.event(self.name, "create_plan", input_summary=user_query, model=meta["model"],
                          retry_count=meta["retries"], target_agent="ExecutorAgent",
                          output_summary=f"scope: {scope_text(scope) or 'city-wide'}; hypotheses: "
                                         f"{[h['dimension'] for h in hyps]}; top_n {top_n}")
        return plan

    def finalise(self, plan: dict, state: MarkovState, draft: dict, evaluation: dict, evidence: Evidence,
                 executor: "ExecutorAgent", revision_count: int, unresolved: bool) -> dict:
        coverage = evaluation["coverage"]
        truncated = any(q["truncated"] for q in evidence.queries.values())
        confidence = 0.25 if not state.findings else 0.35 + 0.5 * min(max(coverage, 0.0), 1.0) + (-0.2 if unresolved else 0.1)
        if truncated:
            confidence -= 0.15
        if executor.degraded:
            confidence -= 0.05 * len(executor.degraded)
        confidence = round(min(max(confidence, 0.05), 0.95), 2)
        caveats = [
            executor.policy_caveat(),
            "Priority 5 is treated as the most urgent (EMERGENCY orders carry it in this extract); the source does not document the priority scale.",
            "The data covers 3 August to 8 September 2026 only, and ages are measured to the last work order raised.",
            "The ranking says where the weighted backlog and overdue work sit; it cannot see crew capacity, contracts or "
            "budgets, which are not in the data.",
        ]
        dq = executor.data_quality
        if dq["unprioritised_orders"]:
            caveats.append(f"{dq['unprioritised_orders']} open orders ({dq['unprioritised_share']:.0%}) have no priority; "
                           f"they are scored as medium urgency until someone triages them.")
        for note in executor.degraded:
            caveats.append(note)
        if executor.sample_weather:
            caveats.append("SAMPLE MODE: the weather table is SYNTHETIC test data, so weather figures are not real.")
        if state.findings and coverage < MIN_COVERAGE:
            caveats.append(f"The hot spots identified hold {coverage:.0%} of the weighted backlog; the rest is spread thinly.")
        if not state.findings:
            caveats.append("No single segment concentrated the backlog; use the city-wide priority list.")
        if unresolved:
            caveats.append("The Evaluator still had open issues when the revision limit was reached.")
        if truncated:
            caveats.append("At least one query hit the row cap, so its result may be incomplete.")

        analysed = {(q["dimension"], "|".join(f"{s['dimension']}={s['member']}" for s in q["scope"][len(plan['scope']):]) or "(all)")
                    for q in evidence.queries.values() if q["kind"] == "breakdown"}
        fb = self.memory.feedback_on_outcome(plan.get("memory_recalled", []), S.MEASURE, state.findings, state.ruled_out, analysed)
        for item in fb:
            self.tracer.event(self.name, "memory_feedback", engine="H-MEM", input_summary=f"prior {item['memory_id']}",
                              output_summary=f"{item['signal']} -> confidence {item['confidence']}")
        stored = self.memory.store_run(plan, executor.enriched, state.ruled_out, unresolved)
        self.tracer.event(self.name, "memory_store", engine="H-MEM", input_summary="verified hot spots and ruled-out dimensions",
                          output_summary=f"{len(stored)} memories written (same-key memories superseded, not deleted)",
                          memory_ids=stored)
        result = {
            "as_of": plan["as_of"], "scope": scope_text(plan["scope"]) or "city-wide", "totals": executor.totals,
            "summary": draft["summary"], "claims": draft["claims"], "findings": executor.public_findings(),
            "priority_list": executor.priority_list(plan["top_n"]), "data_quality": executor.data_quality,
            "calendar": executor.calendar, "history_baseline": executor.history, "policy": executor.policy_summary(), "ruled_out": state.ruled_out, "coverage": coverage, "confidence": confidence,
            "caveats": caveats,
            "memory": {"recalled": [{"id": m["id"], "score": m["score"], "content": m["content"]}
                                    for m in plan.get("memory_recalled", [])],
                       "retrieval_trace": plan.get("memory_trace"), "feedback_applied": fb, "stored": stored},
            "evidence": evidence.public(),
        }
        self.tracer.event(self.name, "final_response", target_agent="User", confidence=confidence,
                          input_summary=f"evaluation: {len(evaluation['issues'])} open issue(s), {revision_count} revision(s)",
                          output_summary=draft["summary"])
        return result


class ExecutorAgent:
    name = "ExecutorAgent"

    def __init__(self, tracer: Tracer, engine: MarkovianEngine, evidence: Evidence, degraded_roles: list[str] | None = None):
        self.tracer, self.engine, self.evidence = tracer, engine, evidence
        self.degraded_roles = degraded_roles or []
        self._plan: dict = {}
        self.base: list[dict] = []
        self.scored: list[dict] = []
        self.raw_rows: list[dict] = []
        self.holidays: set[date] = set()
        self.bad_days: dict[date, str] = {}
        self.holiday_qid = self.weather_qid = self.workset_qid = self.total_qid = self.history_qid = None
        self.history: dict = {}
        self.degraded: list[str] = []
        self.sample_weather = False
        self.totals: dict = {}
        self.data_quality: dict = {}
        self.calendar: dict = {}
        self.enriched: list[dict] = []
        self._last_meta: dict = {}

    # -- data: work set, calendar, weather ------------------------------------------------------------
    def _load_calendar(self, as_of: date) -> None:
        if S.holidays_table() and "holidays" not in self.degraded_roles:
            r = data_access.query(S.holidays_sql(), S.HOLIDAY_COLUMNS, limit=LOOKUP_LIMIT)
            self.holiday_qid = self.evidence.add(S.holidays_sql(), r.rows, r.source, kind="holidays", truncated=r.truncated)
            self.holidays = scoring.holiday_set(r.rows)
            self.tracer.event(self.name, "query_external_source", source=r.source, input_summary="public holidays (web API via Zetaris)",
                              output_summary=f"{len(self.holidays)} holidays")
        else:
            self.degraded.append("The public-holiday table was unavailable, so ages count weekdays only.")
            self.tracer.event(self.name, "external_source_skipped", status="skipped", input_summary="holidays",
                              output_summary="table unavailable; weekdays only")
        if S.weather_table() and "weather" not in self.degraded_roles:
            r = data_access.query(S.weather_sql(), S.WEATHER_COLUMNS, limit=LOOKUP_LIMIT)
            self.weather_qid = self.evidence.add(S.weather_sql(), r.rows, r.source, kind="weather", truncated=r.truncated)
            self.bad_days = scoring.weather_bad_days(r.rows)
            self.sample_weather = r.source == "sample_data"
            self.tracer.event(self.name, "query_external_source", source=r.source, input_summary="daily weather (web API via Zetaris)",
                              output_summary=f"{len(r.rows)} days, {len(self.bad_days)} weather-impacted")
        else:
            self.degraded.append("The weather table was unavailable, so no weather allowance was applied.")
            self.tracer.event(self.name, "external_source_skipped", status="skipped", input_summary="weather",
                              output_summary="table unavailable; no weather allowance")

    def _load_history(self) -> None:
        """Closed historical work orders from the same Zetaris table: the baseline the SLA targets were derived from."""
        if "history" in self.degraded_roles:
            self.degraded.append("No historical work orders were available, so no time-to-close baseline was computed.")
            self.tracer.event(self.name, "external_source_skipped", status="skipped", input_summary="history",
                              output_summary="no history rows; baseline skipped")
            return
        r = data_access.query(S.history_sql(), S.HISTORY_COLUMNS)
        self.history_qid = self.evidence.add(S.history_sql(), r.rows, r.source, kind="history", truncated=r.truncated)
        self.history = scoring.history_baseline(r.rows, self.bad_days)
        w = self.history["weather_effect"]
        self.tracer.event(self.name, "query_data", source=r.source, input_summary="closed historical work orders (baseline)",
                          output_summary=f"{self.history['closed_in_sample']} closed orders raised {self.history['first_raised']} to "
                                         f"{self.history['last_raised']}; weather comparison enough_data={w['enough_data']}")

    def _load_workset(self, plan: dict) -> None:
        sql = S.total_sql(self.base)
        r = data_access.query(sql, S.TOTAL_COLUMNS)
        self.total_qid = self.evidence.add(sql, r.rows, r.source, kind="total", scope=self.base, truncated=r.truncated)
        n, pts = int(analysis.num(r.rows[0]["n_orders"])) if r.rows else 0, analysis.num(r.rows[0]["points"]) if r.rows else 0.0
        if n == 0:
            raise NoDataError(f"No open work orders match {scope_text(self.base) or 'the request'}.", stage="investigate")
        sql = S.workset_sql(self.base)
        limit = int(config.get_float("ZETARIS_WORKSET_LIMIT", 5000))
        w = data_access.query(sql, S.WORKSET_COLUMNS, limit=limit)
        self.workset_qid = self.evidence.add(sql, [], w.source, kind="workset", scope=self.base, truncated=w.truncated)
        self.raw_rows = w.rows
        as_of = date.fromisoformat(plan["as_of"])
        self.scored = [scoring.score_order(row, as_of, self.holidays, self.bad_days) for row in w.rows]
        self.evidence.queries[self.workset_qid]["rows_returned"] = len(w.rows)
        self.tracer.event(self.name, "query_data", source=w.source, input_summary=f"open work orders joined to SLA policy "
                          f"({scope_text(self.base) or 'city-wide'})", output_summary=f"{len(w.rows)} orders; control total {n}"
                          + ("; ROW CAP HIT" if w.truncated else ""))

    # -- Markovian triage loop ---------------------------------------------------------------------------------
    def investigate(self, user_query: str, plan: dict) -> MarkovState:
        self._plan, self.base = plan, [dict(f) for f in plan["scope"]]
        as_of = date.fromisoformat(plan["as_of"])
        self._load_calendar(as_of)
        self._load_history()
        self._load_workset(plan)
        total = self.evidence.queries[self.total_qid]["rows"][0]
        total_pts = analysis.num(total["points"])
        self.totals = self._compute_totals()
        obs = {"type": "total", "open_orders": int(analysis.num(total["n_orders"])), "points": round(total_pts, 2),
               "query_id": self.total_qid}
        fixed = {f["dimension"] for f in self.base}
        dims = [d for d in S.DIMENSIONS if d not in fixed]
        state = self.engine.init_state(user_query, S.MEASURE, plan.get("first_day") or plan["as_of"], plan["as_of"],
                                       total_pts, plan["hypotheses"], dims, None)
        probes: dict[tuple[str, str], dict] = {}
        while True:
            stop, reason = self.engine.should_stop(state)
            if stop:
                state.stopped = state.stopped or reason
                break
            messages = self.engine.build_step_prompt(state, obs)
            action = self._decide(messages, state)
            kind = action["action"]
            if kind == "finish":
                self.engine.record_step(0, {"type": "finish"}, action)
                state.stopped = "model_finished"
                break
            dim = action["dimension"]
            key = (scope_key(state.scope), dim)
            filters = self.base + state.scope
            if kind == "probe":
                sql = S.breakdown_sql(dim, filters)
                r = data_access.query(sql, S.BREAKDOWN_COLUMNS)
                qid = self.evidence.add(sql, r.rows, r.source, dimension=dim, scope=filters, truncated=r.truncated)
                obs = analysis.observation(dim, state.scope, analysis.summarise(r.rows), qid,
                                           raw_chars=len(json.dumps(r.rows, default=str)))
                probes[key] = obs
                self.engine.record_step(obs["raw_chars"], obs, action)
                self.tracer.event(self.name, "query_data", source=r.source,
                                  input_summary=f"probe {dim}, scope={scope_text(filters) or 'city-wide'}",
                                  output_summary=f"{obs['member_count']} members; top={obs['top'][:1]}"
                                                 + ("; ROW CAP HIT" if r.truncated else ""))
            else:
                obs = probes[key]
                self.engine.record_step(0, obs, action)
            new_state = self.engine.transition(state, action, obs)
            added = [f for f in new_state.findings if f["id"] not in {x["id"] for x in state.findings}]
            if kind == "probe":
                pr = new_state.probed[dim]
                text = (f"probed {dim}: top {pr['top_member']} holds {pr['top_share']:.0%} "
                        f"({'concentrated' if pr['concentrated'] else 'diffuse, ruled out'})")
            else:
                text = (f"descend into {dim}: finding(s) {[(f['id'], f['member'], f['share_of_total']) for f in added]}; "
                        f"scope now {scope_text(new_state.scope) or 'all'}")
            self.tracer.event(self.name, "markov_transition", engine="Markovian", input_summary=f"step {new_state.step}",
                              output_summary=text, prompt_chars=self.engine.prompt_chars[-1],
                              carryover_level=self.engine.cascade_levels[-1],
                              carryover_tokens=self.engine.stats()["carryover_tokens_per_step"][-1])
            state = new_state
        self.enrich(state)
        self.tracer.event(self.name, "investigation_complete", engine="Markovian", input_summary=f"{state.step} step(s)",
                          output_summary=f"stopped: {state.stopped}; {len(state.findings)} hot spot(s); "
                                         f"max prompt {max(self.engine.prompt_chars, default=0)} chars",
                          target_agent="EvaluatorAgent")
        return state

    def _decide(self, messages: list[dict], state: MarkovState) -> dict:
        obj, meta = llm.chat_json(messages)
        self.engine.note_usage(meta.get("usage"))
        action, reason, dim = str(obj.get("action", "")).lower(), str(obj.get("reason", ""))[:160], obj.get("dimension")
        options = [d for d, _ in state.descend_options()]
        ok = ((action == "probe" and dim in state.open_dimensions) or (action == "descend" and dim in options)
              or (action == "finish" and (bool(state.findings) or (not state.open_dimensions and not options))))
        if ok:
            decision = {"action": action, "dimension": dim if action != "finish" else None, "reason": reason}
            self.tracer.event(self.name, "decide_next_step", model=meta["model"], retry_count=meta["retries"],
                              input_summary=f"step {state.step}, open={state.open_dimensions}, descend={options}, "
                                            f"prompt_chars={self.engine.prompt_chars[-1]}",
                              output_summary=f"{action} {decision['dimension'] or ''}: {reason}")
            return decision
        self.engine.fallbacks += 1
        if state.open_dimensions:
            decision = {"action": "probe", "dimension": state.open_dimensions[0], "reason": "fallback to first open dimension"}
        elif options:
            decision = {"action": "descend", "dimension": options[0], "reason": "fallback to best descend option"}
        else:
            decision = {"action": "finish", "dimension": None, "reason": "fallback: nothing left to do"}
        self.tracer.event(self.name, "action_fallback", status="fallback", model=meta["model"], retry_count=meta["retries"],
                          input_summary=f"model replied {short(obj, 120)}",
                          output_summary=f"invalid or premature action; using {decision['action']} {decision['dimension']}")
        return decision

    # -- statistics from the scored work set --------------------------------------------------------------------
    def _subset(self, filters: list[dict]) -> list[dict]:
        return [o for o in self.scored if scoring.matches(o, filters)]

    @staticmethod
    def stats(orders: list[dict]) -> dict:
        n = len(orders)
        return {"orders": n, "points": round(sum(o["weight"] for o in orders), 2),
                "overdue_orders": sum(o["overdue"] for o in orders),
                "overdue_net_of_weather_orders": sum(o["overdue_net_of_weather"] for o in orders),
                "unprioritised_orders": sum(o["unprioritised"] for o in orders),
                "oldest_age_working_days": max((o["age_working_days"] for o in orders), default=0)}

    def _compute_totals(self) -> dict:
        return self.stats(self.scored)

    def top_orders(self, orders: list[dict], n: int) -> list[dict]:
        return [{k: o[k] for k in ("work_order_id", "description", "asset_group", "ward", "priority_level",
                                   "age_working_days", "target_days", "overdue", "score", "address")}
                for o in scoring.rank(orders)[:n]]

    def enrich(self, state: MarkovState) -> None:
        n = self._plan.get("top_n", 5)
        for f in state.findings:
            filters = self.base + f["scope"] + [{"dimension": f["dimension"], "member": f["member"]}]
            sub = self._subset(filters)
            f.update(self.stats(sub))
            f["top_orders"] = self.top_orders(sub, min(n, 5))
        self.enriched = state.findings
        orders = self.scored
        unp = [o for o in orders if o["unprioritised"]]
        descs: dict[str, int] = {}
        for o in unp:
            descs[o["description"] or "(none)"] = descs.get(o["description"] or "(none)", 0) + 1
        self.data_quality = {
            "open_orders": len(orders), "unprioritised_orders": len(unp),
            "unprioritised_share": round(len(unp) / len(orders), 4) if orders else 0.0,
            "top_unprioritised_types": [{"description": d, "orders": c} for d, c in sorted(descs.items(), key=lambda kv: -kv[1])[:3]],
            "policy_missing_orders": sum(o["policy_missing"] for o in orders),
        }
        as_of = date.fromisoformat(self._plan["as_of"])
        starts = [date.fromisoformat(o["initiated"]) for o in orders]
        first = min(starts) if starts else as_of
        span_hol = sorted(d for d in self.holidays if first < d <= as_of and d.weekday() < 5)
        span_bad = {d: w for d, w in self.bad_days.items() if first < d <= as_of and d.weekday() < 5 and d not in self.holidays}
        nxt = min((d for d in self.holidays if d > as_of), default=None)
        names = {}
        if self.holiday_qid:
            for r in self.evidence.queries[self.holiday_qid]["rows"]:
                names[scoring.parse_date(r["holiday_date"])] = r.get("holiday_name")
        self.calendar = {
            "as_of": as_of.isoformat(), "span_start": first.isoformat(), "holidays_in_span": len(span_hol),
            "holiday_names_in_span": [names.get(d) for d in span_hol],
            "weather_impacted_working_days": len(span_bad),
            "weather_reasons": sorted(set(span_bad.values())),
            "next_holiday": ({"date": nxt.isoformat(), "name": names.get(nxt), "days_away": (nxt - as_of).days} if nxt else None),
            "thresholds": scoring.WEATHER_THRESHOLDS,
            "overdue_orders": self.totals["overdue_orders"],
            "overdue_net_of_weather_orders": self.totals["overdue_net_of_weather_orders"],
            "weather_data_is_synthetic": self.sample_weather,
        }

    def priority_list(self, n: int) -> list[dict]:
        return self.top_orders(self.scored, n)

    def public_findings(self) -> list[dict]:
        return [{
            "id": f["id"], "level": f["level"], "dimension": f["dimension"], "member": f["member"],
            "scope": scope_text(self.base + f["scope"]) or "city-wide", "orders": f["orders"], "weighted_points": f["points"],
            "share_of_scope_backlog": f["share_of_scope"], "share_of_total_backlog": f["share_of_total"],
            "lift_vs_order_share": f.get("lift"), "overdue_orders": f["overdue_orders"],
            "overdue_net_of_weather_orders": f["overdue_net_of_weather_orders"],
            "unprioritised_orders": f["unprioritised_orders"], "oldest_age_working_days": f["oldest_age_working_days"],
            "top_orders": f["top_orders"], "sources": [f["query_id"]],
        } for f in self.enriched]

    # -- drafting / revising --------------------------------------------------------------------------------------
    def evidence_payload(self, state: MarkovState) -> dict:
        targets = sorted({(o["asset_group"], o["priority_level"], o["target_days"]) for o in self.scored},
                         key=lambda t: (str(t[0]), t[1]))
        return {
            "as_of": self._plan["as_of"], "scope": scope_text(self.base) or "city-wide", "totals": self.totals,
            "findings": [{"id": f["id"], "level": f["level"], "scope": scope_text(self.base + f["scope"]) or "city-wide",
                          "dimension": f["dimension"], "member": f["member"], "orders": f["orders"], "points": f["points"],
                          "share_of_total_backlog": f["share_of_total"], "lift": f.get("lift"),
                          "overdue_orders": f["overdue_orders"],
                          "overdue_net_of_weather_orders": f["overdue_net_of_weather_orders"],
                          "unprioritised_orders": f["unprioritised_orders"],
                          "oldest_age_working_days": f["oldest_age_working_days"],
                          "top_orders": [_brief(t) for t in f["top_orders"][:3]]} for f in state.findings],
            "city_priority_list": [_brief(t) for t in self.priority_list(3)],
            "data_quality": self.data_quality,
            "calendar": {k: v for k, v in self.calendar.items() if k != "thresholds"},
            "policy": self.policy_summary(),
            "history": self.history,
            "ruled_out": [{"dimension": r["dimension"], "scope": r["scope"], "why": r["why"]} for r in state.ruled_out],
            "stop_reason": state.stopped,
        }

    def policy_summary(self) -> dict:
        cells = {}
        for o in self.scored:
            cells[(o["asset_group"], o["priority_level"])] = (o["target_days"], str(o.get("target_basis") or "none"))
        rows = sorted(cells.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))
        emp = sum(1 for _, (_, b) in rows if b.startswith("empirical"))
        return {"unit": "working days", "weights": "assumption (priority 5 most urgent)",
                "targets_from_history_cells": emp, "targets_assumed_cells": len(rows) - emp,
                "targets": [{"asset_group": a, "priority_level": l, "target_days": t,
                             "source": "history" if b.startswith("empirical") else "assumption"} for (a, l), (t, b) in rows]}

    def policy_caveat(self) -> str:
        p = self.policy_summary()
        return (f"SLA targets: {p['targets_from_history_cells']} of the {p['targets_from_history_cells'] + p['targets_assumed_cells']} "
                f"asset-group and priority cells used in this triage use the 75th percentile of closures within {scoring.RESPONSIVE_CAP} working "
                f"days from a sample of {self.history.get('closed_in_sample', 0)} closed historical orders; the rest are assumptions, "
                f"and priority weights are assumptions. The sample is small, so treat targets as indicative, not DC policy.")

    def _write(self, user_query: str, state: MarkovState, version: int, extra: str = "") -> dict:
        user = f"QUESTION: {user_query}\nEVIDENCE_JSON:\n{json.dumps(self.evidence_payload(state), default=str)}{extra}"
        obj, meta = llm.chat_json([{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": user}],
                                  required_keys=("summary", "claims"))
        valid = {f["id"] for f in state.findings} | {"dq", "cal", "hist", "rank"}
        raw = obj["claims"] if isinstance(obj["claims"], list) else []
        claims = [{"finding_id": c["finding_id"], "statement": str(c["statement"])} for c in raw
                  if isinstance(c, dict) and c.get("finding_id") in valid and c.get("statement")]
        self._last_meta = meta
        return {"version": version, "summary": str(obj["summary"]), "claims": claims,
                "dropped_claims": len(raw) - len(claims), "model": meta["model"]}

    def draft(self, user_query: str, state: MarkovState) -> dict:
        d = self._write(user_query, state, 1)
        self.tracer.event(self.name, "draft_created", model=d["model"], retry_count=self._last_meta["retries"],
                          target_agent="EvaluatorAgent", input_summary=f"{len(state.findings)} hot spot(s)",
                          output_summary=d["summary"])
        return d

    def revise(self, user_query: str, state: MarkovState, draft: dict, critique: dict) -> dict:
        extended = 0
        if "coverage" in critique["codes"] and self.can_extend(state):
            extended = self._extend_coverage(state)
            self.enrich(state)
        extra = ("\nPREVIOUS_DRAFT_JSON:\n" + json.dumps({"summary": draft["summary"], "claims": draft["claims"]}) +
                 "\nCRITIQUE_JSON:\n" + json.dumps([i["message"] for i in critique["issues"]]) +
                 "\nRevise the draft to fix every issue. Do not add statements the evidence does not support.")
        r = self._write(user_query, state, draft["version"] + 1, extra)
        self.tracer.event(self.name, "revise_draft", model=r["model"], retry_count=self._last_meta["retries"],
                          target_agent="EvaluatorAgent",
                          input_summary=f"addressing {len(critique['issues'])} issue(s); {extended} extra hot spot(s) added",
                          output_summary=r["summary"])
        return r

    def _level0(self, state: MarkovState) -> list[dict]:
        return [f for f in state.findings if f["level"] == 0]

    def _remaining(self, state: MarkovState) -> list[dict]:
        l0 = self._level0(state)
        if not l0:
            return []
        summary = analysis.summarise(self.evidence.queries[l0[0]["query_id"]]["rows"])
        taken = {f["member"] for f in l0}
        return [m for m in summary["members"] if m["member"] not in taken and m["points"] > 0]

    def can_extend(self, state: MarkovState) -> bool:
        l0 = self._level0(state)
        return bool(l0) and bool(self._remaining(state)) and sum(f["share_of_total"] or 0 for f in l0) < analysis.COVERAGE_TARGET \
            and len(l0) < 5

    def _extend_coverage(self, state: MarkovState) -> int:
        l0 = self._level0(state)
        cum, added = sum(f["share_of_total"] or 0 for f in l0), 0
        for m in self._remaining(state):
            if cum >= analysis.COVERAGE_TARGET or len(l0) + added >= 5:
                break
            state.finding_seq += 1
            share = m["points"] / state.total_delta
            state.findings.append({
                "id": f"f{state.finding_seq}", "level": 0, "dimension": l0[0]["dimension"], "member": m["member"], "scope": [],
                "delta": round(m["points"], 2), "base": m["n_orders"], "compare": round(m["points"], 2),
                "share_of_scope": round(m["share"], 5), "lift": None if m.get("lift") is None else round(m["lift"], 2),
                "share_of_total": round(share, 5), "query_id": l0[0]["query_id"]})
            cum += share
            added += 1
        return added


class EvaluatorAgent:
    name = "EvaluatorAgent"

    def __init__(self, tracer: Tracer):
        self.tracer = tracer

    # 1. the work set the Executor scored must equal Zetaris's own control total for the same filters
    @staticmethod
    def _check_workset(ex: ExecutorAgent, evidence: Evidence, issues: list) -> bool:
        ctl = evidence.queries[ex.total_qid]["rows"][0]
        stats = ExecutorAgent.stats(ex.scored)
        ok = stats["orders"] == int(analysis.num(ctl["n_orders"])) and abs(stats["points"] - analysis.num(ctl["points"])) < 0.01
        if not ok:
            issues.append({"code": "workset_mismatch", "message":
                           f"Work set has {stats['orders']} orders / {stats['points']} points but Zetaris counts "
                           f"{ctl['n_orders']} / {ctl['points']}."})
        return ok

    # 2. every Zetaris GROUP BY must equal the same grouping computed in Python from the work set
    @staticmethod
    def _check_breakdowns(ex: ExecutorAgent, evidence: Evidence, issues: list) -> bool:
        ok = True
        for qid, q in evidence.queries.items():
            if q["kind"] != "breakdown":
                continue
            mine = group(ex.scored, q["dimension"], q["scope"])
            theirs = {analysis.label(r["member"]): (int(analysis.num(r["n_orders"])), analysis.num(r["points"])) for r in q["rows"]}
            mine_t = {k: (v["n_orders"], v["points"]) for k, v in mine.items()}
            if set(mine_t) != set(theirs) or any(mine_t[k][0] != theirs[k][0] or abs(mine_t[k][1] - theirs[k][1]) > 0.01 for k in theirs if k in mine_t):
                ok = False
                issues.append({"code": "sum_mismatch", "message": f"{qid} ({q['dimension']}) from Zetaris does not match the recomputation from the work set."})
        return ok

    # 3. each hot spot: recompute its numbers from the work set AND from a fresh Zetaris query
    @staticmethod
    def _check_findings(ex: ExecutorAgent, state: MarkovState, issues: list) -> bool:
        ok = True
        total = ex.totals["points"]
        for f in state.findings:
            filters = ex.base + f["scope"] + [{"dimension": f["dimension"], "member": f["member"]}]
            mine = ExecutorAgent.stats(ex._subset(filters))
            if any(abs(mine[k] - f[k]) > 1e-6 for k in mine if k in f):
                ok = False
                issues.append({"code": "integrity", "message": f"Hot spot {f['id']} statistics do not recompute from the work set."})
            fresh = data_access.query(S.total_sql(filters), S.TOTAL_COLUMNS).rows
            if not fresh or int(analysis.num(fresh[0]["n_orders"])) != f["orders"] or abs(analysis.num(fresh[0]["points"]) - f["points"]) > 0.01:
                ok = False
                issues.append({"code": "integrity", "message": f"Hot spot {f['id']} differs from a fresh Zetaris query."})
            if total > 0 and f.get("share_of_total") is not None and f["level"] == 0 and abs(f["points"] / total - f["share_of_total"]) > 0.002:
                ok = False
                issues.append({"code": "integrity", "message": f"Hot spot {f['id']} share of the backlog does not recompute."})
        return ok

    # 4. the ranking: rescore from raw fields, order must be descending and inside scope
    @staticmethod
    def _check_ranking(ex: ExecutorAgent, issues: list) -> bool:
        ok = True
        as_of = date.fromisoformat(ex._plan["as_of"])
        raw = {str(scoring.wid(r["work_order_id"])): r for r in ex.raw_rows}
        lists = [("city-wide list", ex.priority_list(ex._plan["top_n"]), ex.base)] + \
                [(f["id"], f["top_orders"], ex.base + f["scope"] + [{"dimension": f["dimension"], "member": f["member"]}])
                 for f in ex.enriched]
        for name, lst, filters in lists:
            scores = [t["score"] for t in lst]
            if scores != sorted(scores, reverse=True):
                ok = False
                issues.append({"code": "ranking", "message": f"{name} is not in descending score order."})
            for t in lst:
                row = raw.get(str(t["work_order_id"]))
                again = scoring.score_order(row, as_of, ex.holidays, ex.bad_days) if row else None
                if again is None or abs(again["score"] - t["score"]) > 0.01 or not scoring.matches(again, filters):
                    ok = False
                    issues.append({"code": "ranking", "message": f"{name}: order {t['work_order_id']} does not re-score or is outside scope."})
                    break
        return ok

    # 5. calendar inputs: a fresh Zetaris read of holidays and weather must give the same sets
    @staticmethod
    def _check_calendar(ex: ExecutorAgent, evidence: Evidence, issues: list) -> bool | None:
        if ex.holiday_qid is None and ex.weather_qid is None:
            return None
        ok = True
        if ex.holiday_qid:
            fresh = scoring.holiday_set(data_access.query(S.holidays_sql(), S.HOLIDAY_COLUMNS, limit=LOOKUP_LIMIT).rows)
            if fresh != ex.holidays:
                ok = False
                issues.append({"code": "calendar_mismatch", "message": "Holidays re-read from Zetaris differ from those used."})
        if ex.weather_qid:
            fresh_bad = scoring.weather_bad_days(data_access.query(S.weather_sql(), S.WEATHER_COLUMNS, limit=LOOKUP_LIMIT).rows)
            if set(fresh_bad) != set(ex.bad_days):
                ok = False
                issues.append({"code": "calendar_mismatch", "message": "Weather days re-read from Zetaris differ from those used."})
        return ok

    # 6. history baseline: a fresh Zetaris read must reproduce the same statistics
    @staticmethod
    def _check_history(ex: ExecutorAgent, issues: list) -> bool | None:
        if ex.history_qid is None:
            return None
        fresh = scoring.history_baseline(data_access.query(S.history_sql(), S.HISTORY_COLUMNS).rows, ex.bad_days)
        ok = fresh == ex.history
        if not ok:
            issues.append({"code": "history_mismatch", "message": "The historical baseline re-read from Zetaris differs from the one used."})
        return ok

    @staticmethod
    def _check_labels(payload: dict, draft: dict, issues: list) -> bool:
        """A claim about hot spot fN may name a priority level only if fN's own dimension, scope or top orders carry it
        (live run: a UFA-in-ward-7 hot spot was described as 'priority level 0')."""
        by_id = {f["id"]: f for f in payload["findings"]}
        ok = True
        for c in draft["claims"]:
            f = by_id.get(c["finding_id"])
            if not f:
                continue
            allowed = {str(t["priority_level"]) for t in f["top_orders"]}
            if f["dimension"] == "priority_level":
                allowed.add(str(f["member"]))
            allowed |= set(re.findall(r"priority_level=(\d)", f["scope"]))
            for n in re.findall(r"priority[\s\-\u2010-\u2015]*level[\s\-\u2010-\u2015]*(\d)", c["statement"], re.I):
                if n not in allowed:
                    ok = False
                    issues.append({"code": "label_mismatch", "message":
                                   f"Claim for {f['id']} says priority level {n}, but {f['id']} is {f['dimension']}={f['member']}"
                                   f" (scope {f['scope']}); its orders carry priority {sorted(allowed)}."})
        return ok

    def review(self, user_query: str, state: MarkovState, draft: dict, evidence: Evidence, ex: ExecutorAgent) -> dict:
        issues: list[dict] = []
        workset_ok = self._check_workset(ex, evidence, issues)
        sums_ok = self._check_breakdowns(ex, evidence, issues)
        integrity_ok = self._check_findings(ex, state, issues)
        ranking_ok = self._check_ranking(ex, issues)
        calendar_ok = self._check_calendar(ex, evidence, issues)
        history_ok = self._check_history(ex, issues)
        capped = [qid for qid, q in evidence.queries.items() if q["truncated"]]
        if capped:
            issues.append({"code": "truncated", "message": f"Row cap hit in {capped}; results may be incomplete."})
        cited = {c["finding_id"] for c in draft["claims"]}
        missing = [f["id"] for f in state.findings if f["id"] not in cited]
        if missing:
            issues.append({"code": "uncited_finding", "message": f"No claim cites hot spot(s) {missing}."})
        if not draft["summary"].strip():
            issues.append({"code": "empty_summary", "message": "The summary is empty."})
        dq_needed = ex.data_quality["unprioritised_orders"] > 0
        dq_ok = (not dq_needed) or "dq" in cited
        if not dq_ok:
            issues.append({"code": "data_quality_omitted", "message":
                           f"{ex.data_quality['unprioritised_orders']} open orders have no priority; add a claim with finding_id \"dq\"."})
        payload = ex.evidence_payload(state)
        top = payload["city_priority_list"][:1]
        text = " ".join([draft["summary"]] + [c["statement"] for c in draft["claims"]])
        labels_ok = self._check_labels(payload, draft, issues)
        lead_ok = (not top) or str(top[0]["work_order_id"]) in text
        if not lead_ok:
            issues.append({"code": "rank_omitted", "message":
                           f"The top-ranked order (work order {top[0]['work_order_id']}, {top[0]['description']}) is not named; "
                           f"begin the summary with city_priority_list and add a claim with finding_id \"rank\"."})
        loose = analysis.ungrounded_numbers(text, payload)
        if loose:
            issues.append({"code": "ungrounded_number", "message":
                           f"Numbers not found in the evidence: {sorted(set(loose))[:6]}. Restate using evidence numbers only."})
        l0 = [f for f in state.findings if f["level"] == 0]
        coverage = round(sum(f["points"] for f in l0) / ex.totals["points"], 3) if l0 and ex.totals["points"] else 0.0
        if l0 and coverage < MIN_COVERAGE and ex.can_extend(state):
            issues.append({"code": "coverage", "message": f"Cited hot spots hold only {coverage:.0%} of the weighted backlog; add the next-largest."})
        user = (f"QUESTION: {user_query}\nEVIDENCE_JSON:\n{json.dumps(payload, default=str)}\n"
                f"DRAFT_JSON:\n{json.dumps({'summary': draft['summary'], 'claims': draft['claims']})}")
        critic_down = False
        try:
            obj, meta = llm.chat_json([{"role": "system", "content": CRITIC_SYSTEM}, {"role": "user", "content": user}],
                                      required_keys=("requires_revision", "issues"))
        except LLMError as exc:        # the deterministic checks above still stand; say the model critic was unavailable
            critic_down = True
            obj, meta = {"requires_revision": False, "issues": []}, {"model": None, "retries": 0}
            self.tracer.event(self.name, "model_critic_unavailable", status="degraded", output_summary=short(str(exc), 160))
        critic = [str(i) for i in obj["issues"]] if isinstance(obj["issues"], list) else []
        critic = [i for i in critic if not _META.search(i)]   # complaints about the reply format are not review findings
        if not critic:
            obj["requires_revision"] = False
        if obj["requires_revision"] and critic:
            issues += [{"code": "unsupported_claim", "message": i} for i in critic[:5]]
        requires = bool(issues)
        review = {
            "requires_revision": requires, "issues": issues, "codes": [i["code"] for i in issues], "coverage": coverage,
            "model": meta["model"],
            "checks": {"workset_matches_zetaris": workset_ok, "breakdowns_recomputed": sums_ok, "hotspots_recomputed": integrity_ok,
                       "ranking_rescored": ranking_ok, "calendar_reread": calendar_ok, "history_reread": history_ok, "not_truncated": not capped,
                       "numbers_grounded": not loose, "leads_with_top_ranked": lead_ok, "claim_labels_match": labels_ok, "all_findings_cited": not missing,
                       "data_quality_disclosed": dq_ok, "coverage": coverage, "model_critic_flags": None if critic_down else len(critic)},
            "critic_unavailable": critic_down,
        }
        self.tracer.event(self.name, "critique_issued" if requires else "approve_draft", model=meta["model"],
                          retry_count=meta["retries"], target_agent="ExecutorAgent" if requires else "PlannerAgent",
                          input_summary=f"draft v{draft['version']}",
                          output_summary="; ".join(i["message"] for i in issues) if requires else "all checks passed",
                          checks=review["checks"])
        return review
