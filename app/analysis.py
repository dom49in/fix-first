"""Concentration analysis over a priority-weighted open backlog, the evidence ledger and the grounding check.

Measure: points = sum of the SLA policy weight of each open work order. For a breakdown, each member has n_orders,
points, share (of the scope's points) and lift = share of points / share of orders (>1 means the member is heavier
than its size, e.g. more high-priority work). Pure functions over query rows.
"""
from __future__ import annotations

import re

EPS = 1e-9
CONCENTRATION_THRESHOLD = 0.30   # the top member must hold >= 30% of the scope's weighted backlog
COVERAGE_TARGET = 0.80           # level-0 findings are added until they hold >= 80% of the backlog
MAX_CONTRIBUTORS = 3


def num(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def label(member) -> str:
    if member is None:
        return "(none)"
    if isinstance(member, float) and member.is_integer():
        return str(int(member))
    return str(member)


def summarise(rows: list[dict]) -> dict:
    members = [{"member": label(r.get("member")), "n_orders": int(num(r.get("n_orders"))), "points": num(r.get("points"))}
               for r in rows]
    pts, orders = sum(m["points"] for m in members), sum(m["n_orders"] for m in members)
    for m in members:
        m["share"] = m["points"] / pts if pts > EPS else None
        order_share = m["n_orders"] / orders if orders else None
        m["lift"] = (m["share"] / order_share) if (m["share"] is not None and order_share) else None
    members.sort(key=lambda m: -m["points"])
    return {"members": members, "points": pts, "n_orders": orders}


def contributors(summary: dict, max_n: int = MAX_CONTRIBUTORS, target: float = COVERAGE_TARGET) -> list[dict]:
    out, cum = [], 0.0
    for m in summary["members"]:
        if m["points"] <= EPS:
            continue
        out.append(m)
        cum += m["share"] or 0.0
        if cum >= target or len(out) >= max_n:
            break
    return out


def _engine_member(m: dict) -> dict:
    """Shape the engine expects: delta = points, base = orders, compare = points."""
    return {"member": m["member"], "base": m["n_orders"], "compare": round(m["points"], 2),
            "delta": round(m["points"], 2), "share": None if m["share"] is None else round(m["share"], 5),
            "lift": None if m.get("lift") is None else round(m["lift"], 2)}


def observation(dimension: str, scope: list[dict], summary: dict, query_id: str, raw_chars: int) -> dict:
    members = summary["members"]
    top = members[0] if members else None
    concentrated, why = False, ""
    if top is None or summary["points"] <= EPS:
        why = "no open work in scope"
    elif len(members) == 1:
        why = f"only one member ('{top['member']}') in scope, so it adds no information"
    elif top["share"] >= CONCENTRATION_THRESHOLD:
        concentrated = True
    else:
        why = f"diffuse: largest member '{top['member']}' holds {top['share']:.0%} of the scope's weighted backlog"
    return {
        "type": "breakdown", "dimension": dimension, "scope": [dict(s) for s in scope],
        "scope_delta": round(summary["points"], 2), "member_count": len(members),
        "top": [_engine_member(m) for m in members[:5]], "concentrated": concentrated,
        "contributors": [_engine_member(m) for m in contributors(summary)] if concentrated else [],
        "why": why, "query_id": query_id, "raw_chars": raw_chars,
    }


class Evidence:
    """Ledger of every query run, with its rows, so claims can be re-verified independently."""

    def __init__(self) -> None:
        self.queries: dict[str, dict] = {}
        self._n = 0

    def add(self, sql: str, rows: list[dict], source: str, dimension: str | None = None,
            scope: list[dict] | None = None, kind: str = "breakdown", truncated: bool = False) -> str:
        qid = f"q{self._n}"
        self._n += 1
        self.queries[qid] = {"sql": sql, "rows": rows, "source": source, "dimension": dimension, "kind": kind,
                             "scope": [dict(s) for s in (scope or [])], "truncated": truncated}
        return qid

    def first(self, kind: str) -> str:
        for qid, q in self.queries.items():
            if q["kind"] == kind:
                return qid
        raise KeyError(f"no {kind} query recorded")

    def public(self) -> list[dict]:
        return [{"query_id": qid, "kind": q["kind"], "sql": q["sql"], "rows_returned": q.get("rows_returned", len(q["rows"])),
                 "source": q["source"], "dimension": q["dimension"], "scope": q["scope"],
                 "truncated": q["truncated"]}
                for qid, q in self.queries.items()]



# ---- deterministic draft checks ----
_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b(?:19|20)\d{2}\b")
_NUMBER = re.compile(r"(?<![\w.])(\$?)(\d[\d,]*(?:\.\d+)?)\s?(%|k\b|m\b|bn?\b)?", re.I)


def numbers_in_text(text: str) -> list[dict]:
    """Numbers a reader would take as claims. Dates, ids and bare small integers (counts) are ignored."""
    cleaned = _DATE.sub(" ", text)
    out = []
    for m in _NUMBER.finditer(cleaned):
        if re.match(r"\s?(?:st|nd|rd|th)\b", cleaned[m.end():m.end() + 4], re.I):
            continue   # ordinals such as "75th percentile" are wording, not claims
        raw = m.group(2).replace(",", "")
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        suffix = (m.group(3) or "").lower()
        value = float(raw)
        if not suffix and decimals == 0 and value <= 10 and not m.group(1):
            continue
        out.append({"text": m.group(0).strip(), "value": value, "decimals": decimals, "suffix": suffix})
    return out


def _leaf_numbers(obj, acc: set) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        acc.add(abs(float(obj)))
    elif isinstance(obj, dict):
        for v in obj.values():
            _leaf_numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _leaf_numbers(v, acc)


def ungrounded_numbers(text: str, payload: dict) -> list[str]:
    """Numbers in `text` that are not a rounding of any number in the evidence payload."""
    known: set[float] = set()
    _leaf_numbers(payload, known)
    bad = []
    for n in numbers_in_text(text):
        scale = {"": 1.0, "%": 1.0, "k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}[n["suffix"]]
        value, tol = n["value"] * scale, 0.5 * 10 ** (-n["decimals"]) * scale + 1e-9
        ok = False
        for e in known:
            # a share stored as 0.463 may be written "46%" or "46.3%", so compare against e and e*100
            if any(abs(value - c) <= tol for c in (e, e * 100)):
                ok = True
                break
        if not ok:
            bad.append(n["text"])
    return bad


