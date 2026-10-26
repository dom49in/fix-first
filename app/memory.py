"""Domain glue between the agents and the H-MEM engine (app/engines/hmem.py).

Planner side: recall past confirmed hot spots (where weighted backlog concentrated) and ruled-out dimensions, as HINTS (never evidence).
Finalise side: score each recalled memory against what this run actually found (feedback), then mine this
run's verified findings into new memories. Every step is written to H-MEM's trust ledger.
"""
from __future__ import annotations

from pathlib import Path

from . import config
from .engines.hmem import HMem
from .schema import DOMAIN_ALIASES, MEASURE


def default_dir() -> Path:
    base = Path(config.get("MEMORY_DIR", ".memory"))
    return (base if base.is_absolute() else config.ROOT / base) / "hmem"


def _scope_key(scope: list[dict]) -> str:
    return "|".join(f"{s['dimension']}={s['member']}" for s in scope) or "(all)"


class InvestigationMemory:
    def __init__(self, hmem: HMem | None = None, directory: Path | str | None = None):
        self.enabled = (config.get("MEMORY_ENABLED", "true") or "true").lower() != "false"
        self.hmem = hmem if hmem is not None else (HMem(directory or default_dir()) if self.enabled else None)

    @staticmethod
    def threshold() -> float:
        """Spec default is 0.35 (HMem.RETRIEVAL_THRESHOLD). The spec says to tune per domain: here memories are short,
        machine-written and questions rarely name an entity (the 0.15 entity term is then 0), so the best
        relevant memory scores ~0.29. Default 0.25; override with HMEM_THRESHOLD."""
        return config.get_float("HMEM_THRESHOLD", 0.25)

    # -- recall ---------------------------------------------------------------------------------
    def recall(self, query: str) -> dict:
        if not self.enabled or self.hmem is None:
            return {"memories": [], "context": "", "trace": {"retrievalReason": "memory disabled", "returned": []}}
        return self.hmem.retrieve(query, actor="PlannerAgent", threshold=self.threshold(),
                                  domain_aliases=DOMAIN_ALIASES)

    @staticmethod
    def hints(recall: dict, limit: int = 5) -> list[dict]:
        return [{"id": m["id"], "score": m["score"], "content": m["content"][:240], "facts": m.get("facts", {}),
                 "confidence": m["confidence"]} for m in recall["memories"][:limit]]

    # -- feedback on recalled memories ------------------------------------------------------------
    def feedback_on_outcome(self, recalled: list[dict], metric: str, findings: list[dict], ruled_out: list[dict],
                            analysed: set[tuple[str, str]]) -> list[dict]:
        """helpful when the prior still holds on today's data, not_helpful when the data contradicts it.
        `analysed` is the set of (dimension, scope_key) breakdowns this run actually queried."""
        if not self.enabled or self.hmem is None:
            return []
        found = {(f["dimension"], f["member"], _scope_key(f["scope"])) for f in findings}
        found_dims = {(f["dimension"], _scope_key(f["scope"])) for f in findings}
        ruled = {(r["dimension"], r["scope"]) for r in ruled_out}
        out = []
        for m in recalled:
            facts = m.get("facts") or {}
            if facts.get("metric") != metric:
                continue
            signal = None
            if facts.get("kind") == "confirmed_driver":
                key = (facts["dimension"], facts["member"], _scope_key(facts.get("scope", [])))
                if key in found:
                    signal = "helpful"
                elif (facts["dimension"], _scope_key(facts.get("scope", []))) in analysed:
                    signal = "not_helpful"
            elif facts.get("kind") == "ruled_out":
                key2 = (facts["dimension"], facts.get("scope_key", "(all)"))
                if key2 in ruled:
                    signal = "helpful"
                elif key2 in found_dims:
                    signal = "not_helpful"
            if signal:
                mem = self.hmem.feedback(m["id"], signal, actor="PlannerAgent",
                                         note="prior checked against this run's queries")
                out.append({"memory_id": m["id"], "signal": signal, "confidence": round(mem.confidence, 3)})
        return out

    # -- mining ---------------------------------------------------------------------------------------
    def store_run(self, plan: dict, findings: list[dict], ruled_out: list[dict], unresolved: bool) -> list[str]:
        if not self.enabled or self.hmem is None:
            return []
        metric, as_of = MEASURE, plan["as_of"]
        base = " AND ".join(f"{f['dimension']}={f['member']}" for f in plan.get("scope", [])) or "all open work"
        haircut = 0.8 if unresolved else 1.0
        stored = []
        for f in findings:
            if f.get("share_of_total") is None:
                continue
            scope = f["scope"]
            where = " AND ".join(f"{s['dimension']}={s['member']}" for s in scope)
            where = (where + " AND ") if where else ""
            share = f["share_of_total"]
            content = (f"Maintenance triage of {base} as of {as_of}: {where}{f['dimension']}={f['member']} held "
                       f"{share:.0%} of the priority-weighted open backlog ({f.get('orders', '?')} orders, "
                       f"{f.get('overdue_orders', '?')} past their SLA target).")
            mem = self.hmem.add(
                content, source="evaluator_approved_run" if not unresolved else "unresolved_run",
                type="factual", layer="working", confidence=round(haircut * (0.6 + 0.35 * min(abs(share), 1.0)), 2),
                tags=[metric, "hotspot", f["dimension"], "backlog", "triage"],
                domain=metric, namespace=f"{metric}/hotspots",
                entities=[f["member"], *[s["member"] for s in scope]], origin="plan_completion",
                label=f"hotspot as of {as_of}",
                facts={"kind": "confirmed_driver", "metric": metric, "periods": as_of, "dimension": f["dimension"],
                       "member": f["member"], "scope": scope, "share_of_total": share},
                replace_key=f"hotspot|{base}|{_scope_key(scope)}|{f['dimension']}={f['member']}",
                actor="PlannerAgent")
            stored.append(mem.id)
        for r in ruled_out:
            content = (f"Maintenance triage of {base} as of {as_of}: dimension {r['dimension']} (scope {r['scope']}) "
                       f"did not concentrate the weighted backlog ({str(r['why'])[:80]}).")
            mem = self.hmem.add(
                content, source="evaluator_approved_run" if not unresolved else "unresolved_run",
                type="factual", layer="working", confidence=round(haircut * 0.7, 2),
                tags=[metric, "ruled_out", r["dimension"], "backlog", "triage"],
                domain=metric, namespace=f"{metric}/ruled_out",
                entities=[], origin="plan_completion", label=f"ruled out as of {as_of}",
                facts={"kind": "ruled_out", "metric": metric, "periods": as_of, "dimension": r["dimension"],
                       "scope_key": r["scope"]},
                replace_key=f"ruled|{base}|{r['scope']}|{r['dimension']}", actor="PlannerAgent")
            stored.append(mem.id)
        return stored
