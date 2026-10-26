"""Offline rules: a deterministic stand-in for the model, used ONLY when SAMPLE_MODE=true and Nightshift credentials are
absent (see app/llm.py: mode()). It is not a model and is never logged as one: the model field is "offline-rules".

Each role reads the same prompt a model would receive and applies explicit rules to it:
  planner       parse a ward / asset group from the question; order hypotheses (recalled H-MEM hot spots first)
  step decider  probe the first OPEN dimension, then descend into the best option, then finish
  drafter       one claim per hot spot plus a data-quality and a calendar claim, using only numbers in the evidence
  critic        flag causal assertions the data cannot support
Nothing here stores or returns a prewritten answer: every string is built from the prompt it is given.
"""
from __future__ import annotations

import json
import re

from .errors import LLMError

_GROUPS = [(r"street ?light|lighting|lamp", "Street Light Network"), (r"\btree|\bufa\b|urban forest|pruning|planting", "UFA"),
           (r"\btoa\b|transportation|sign|pothole|pavement", "TOA"), (r"amis|rmcs", "AMIS & RMCS")]
_CAUSAL = re.compile(r"\b(because|due to|caused by|as a result of|owing to)\b", re.I)
_CAUSES = re.compile(r"\b(shortage|budget|contract|strike|understaffed|staffing)\b", re.I)


def _label(text: str, label: str):
    m = re.search(rf"{label}:\s*", text)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[m.end():])
        return value
    except ValueError:
        return None


def _line(text: str, label: str) -> str | None:
    m = re.search(rf"^{label}:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def respond(messages: list[dict]) -> dict:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    role = (_line(system, "ROLE") or "").strip()
    handler = {"planner": _plan, "markovian-step-decider": _step, "executor-drafter": _draft,
               "evaluator-critic": _critic}.get(role)
    if handler is None:
        raise LLMError(f"Offline rules have no handler for role {role!r}.", stage="llm")
    return handler(user)


def _plan(user: str) -> dict:
    question = (_line(user, "QUESTION") or "").lower()
    profile = _label(user, "PROFILE_JSON \\(open orders per member\\)") or {}
    memory = _label(user, "PAST_MEMORY_JSON") or []
    scope = []
    m = re.search(r"ward\s*#?\s*(\d)", question)
    if m and m.group(1) in (profile.get("ward") or {}):
        scope.append({"dimension": "ward", "member": m.group(1)})
    for pattern, name in _GROUPS:
        if re.search(pattern, question) and name in (profile.get("asset_group") or {}) and len(scope) < 2:
            scope.append({"dimension": "asset_group", "member": name})
            break
    if re.search(r"no priority|unprioriti|without (a )?priority|missing priority|blind", question) \
            and "0" in (profile.get("priority_level") or {}) and len(scope) < 2:
        scope.append({"dimension": "priority_level", "member": "0"})
    fixed = {s["dimension"] for s in scope}
    order = ["asset_group", "ward", "category", "priority_level"]
    if re.search(r"priorit|triage|unprioriti|missing|blind", question):
        order = ["priority_level", "asset_group", "category", "ward"]
    confirmed = [h["facts"]["dimension"] for h in memory if h.get("facts", {}).get("kind") == "confirmed_driver"]
    ruled = [h["facts"]["dimension"] for h in memory if h.get("facts", {}).get("kind") == "ruled_out"]
    order = [d for d in confirmed if d in order] + [d for d in order if d not in confirmed and d not in ruled] \
        + [d for d in order if d in ruled and d not in confirmed]
    seen, hyps = set(), []
    for d in order:
        if d not in seen and d not in fixed:
            seen.add(d)
            hyps.append({"dimension": d, "rationale": "recalled prior" if d in confirmed else "default order"})
    top_n = 10 if re.search(r"top 10|ten ", question) else 5
    return {"scope": scope, "hypotheses": hyps, "top_n": top_n,
            "success_criteria": "Name where weighted backlog concentrates and which orders crews should take first."}


def _step(user: str) -> dict:
    carry = user.split("LATEST_OBSERVATION_JSON:")[0]
    covered = re.search(r"covered (\d+)% of total weighted backlog", carry)
    open_dims = (_line(carry.replace("; ", "\n"), "OPEN to probe") or "none")
    open_dims = [d.strip() for d in open_dims.split(",") if d.strip() and d.strip() != "none"]
    options = (_line(carry.replace("; ", "\n"), "DESCEND options") or "none")
    options = [re.sub(r"\s*\(.*$", "", o).strip() for o in options.split(",") if o.strip() and o.strip() != "none"]
    if covered and int(covered.group(1)) >= 90:
        return {"action": "finish", "dimension": None, "reason": "hot spots already cover the backlog"}
    if open_dims:
        return {"action": "probe", "dimension": open_dims[0], "reason": "next open dimension"}
    if options:
        return {"action": "descend", "dimension": options[0], "reason": "best concentrated option"}
    return {"action": "finish", "dimension": None, "reason": "nothing left to probe"}


def _where(f: dict) -> str:
    return f"{f['dimension']}={f['member']}" + ("" if f["scope"] == "city-wide" else f" within {f['scope']}")


def _draft(user: str) -> dict:
    ev = _label(user, "EVIDENCE_JSON") or {}
    t, dq, cal = ev.get("totals", {}), ev.get("data_quality", {}), ev.get("calendar", {})
    findings = ev.get("findings", [])
    rank = ev.get("city_priority_list") or []
    lead = ""
    if rank:
        r0 = rank[0]
        lead = (f"Take work order {r0['work_order_id']} ({r0['description']}, priority {r0['priority_level']}) first: open "
                f"{r0['age_working_days']} working days against a {r0['target_days']}-day target"
                + (f", {r0['working_days_over_target']} over." if r0.get("overdue") else ".") + " ")
    summary = (lead + f"{t.get('orders')} open work orders ({ev.get('scope')}) carry {t.get('points')} priority-weighted points as of "
               f"{ev.get('as_of')}; past their SLA target: {t.get('overdue_orders')}.")
    if findings:
        top = max(findings, key=lambda f: f["points"])
        summary += (f" The heaviest hot spot is {_where(top)} with {top['share_of_total_backlog']:.0%} of the weighted "
                    f"backlog; send crews there first.")
    else:
        summary += " No single segment concentrates the backlog, so use the city-wide priority list."
    claims = []
    if rank:
        claims.append({"finding_id": "rank", "statement": "Ranked first: " + "; ".join(
            f"work order {r['work_order_id']} ({r['description']}, {r['age_working_days']} days open vs {r['target_days']}-day target)"
            for r in rank) + "."})
    for f in findings:
        lead = f["top_orders"][0] if f["top_orders"] else None
        line = (f"{_where(f)}: {f['orders']} open order(s), {f['overdue_orders']} past target "
                f"({f['overdue_net_of_weather_orders']} once weather-impacted days are allowed for); the oldest has waited "
                f"{f['oldest_age_working_days']} working days.")
        if lead:
            line += (f" Start with work order {lead['work_order_id']} ({lead['description']}), open "
                     f"{lead['age_working_days']} working days against a {lead['target_days']}-day target.")
        claims.append({"finding_id": f["id"], "statement": line})
    if dq.get("unprioritised_orders"):
        types = dq.get("top_unprioritised_types") or []
        extra = f", mostly {types[0]['description']} ({types[0]['orders']} orders)" if types else ""
        claims.append({"finding_id": "dq", "statement":
                       f"{dq['unprioritised_orders']} open orders ({dq['unprioritised_share']:.0%}) have no priority recorded{extra}; "
                       f"they need triage before they can be ranked fairly."})
    hist = ev.get("history") or {}
    groups = {g["asset_group"]: g for g in hist.get("by_asset_group", [])}
    lead = next((f for f in findings if f["dimension"] == "asset_group"), None)
    grp = groups.get(lead["member"]) if lead else None
    grp = grp or (max(groups.values(), key=lambda g: g["closed_in_sample"]) if groups else None)
    if grp and grp.get("median_working_days_within_cap") is not None:
        enough = hist.get("weather_effect", {}).get("enough_data")
        claims.append({"finding_id": "hist", "statement":
                       f"In the closed historical sample, {grp['asset_group']} orders that closed within "
                       f"{hist['responsive_cap_working_days']} working days took a median of "
                       f"{grp['median_working_days_within_cap']:g} working days ({grp['closed_in_sample']} closed in the sample). "
                       + ("" if enough else "The sample is too small to tell whether weather slows closures.")})
    claims.append({"finding_id": "cal", "statement":
                   f"Ages count working days only: {cal.get('holidays_in_span')} public holiday(s) and "
                   f"{cal.get('weather_impacted_working_days')} weather-impacted day(s) fall in the period; allowing for "
                   f"weather, overdue orders drop from {cal.get('overdue_orders')} to {cal.get('overdue_net_of_weather_orders')}."})
    return {"summary": summary, "claims": claims}


def _critic(user: str) -> dict:
    draft = _label(user, "DRAFT_JSON") or {}
    text = " ".join([draft.get("summary", "")] + [c.get("statement", "") for c in draft.get("claims", [])])
    issues = []
    if _CAUSAL.search(text) and _CAUSES.search(text):
        issues.append("The draft asserts a cause (staffing, budget or contracts) that the data cannot show.")
    return {"requires_revision": bool(issues), "issues": issues}
