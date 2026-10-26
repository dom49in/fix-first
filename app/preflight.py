"""Pre-flight data check. The agents are only allowed to run once Zetaris confirms the data is there.

For every table the agents read it runs  SELECT COUNT(1) AS n FROM <table>  through the same adapter the agents use.
  work_orders  REQUIRED: n must be > 0 and, if data/manifest.json says how many rows were loaded, equal to it
               (a half-loaded UNION view fails here instead of silently giving wrong answers).
  sla_policy   REQUIRED (the SLA targets and weights come from it) unless PREFLIGHT_REQUIRE_POLICY=false.
  history      OPTIONAL: closed historical rows in the same table (record_set = history_sample); without them there is no
               time-to-close baseline.
  holidays, weather   OPTIONAL: when missing or empty the run continues in a degraded mode that the answer states
               (weekends-only working days; no weather allowance). PREFLIGHT_REQUIRE_ALL=true makes them required.
"""
from __future__ import annotations

import json
import time

from . import config, data_access, schema
from .errors import AppError, DataNotReadyError

MANIFEST = config.ROOT / "data" / "manifest.json"
REMEDY = ("Load the data into Zetaris first:  python scripts/load_to_zetaris.py  (work orders incl. history + SLA policy), then register the "
          "web-API tables:  python scripts/load_to_zetaris.py --register-apis  . See README 'Load the data'.")


def expected(key: str) -> int | None:
    try:
        return int(json.loads(MANIFEST.read_text())[key])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def expected_work_orders() -> int | None:
    return expected("work_orders_rows")


def _flag(name: str, default: str = "false") -> bool:
    return (config.get(name, default) or default).lower() == "true"


def _count(table: str, sql: str | None = None) -> dict:
    t0 = time.time()
    try:
        res = data_access.query(sql or schema.count_sql(table), ["n"])
        n = int(float(res.rows[0]["n"])) if res.rows else 0
        return {"table": table, "rows": n, "status": "ok" if n > 0 else "empty", "ms": int((time.time() - t0) * 1000)}
    except AppError as exc:
        return {"table": table, "rows": None, "status": "error", "error": exc.message[:200],
                "ms": int((time.time() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001 - a failed probe must become a report, never a crash
        return {"table": table, "rows": None, "status": "error", "error": f"{type(exc).__name__}: {exc}"[:200], "ms": 0}


def run_preflight() -> dict:
    require_all = _flag("PREFLIGHT_REQUIRE_ALL")
    plan = [("work_orders", schema.work_orders_table(), True),
            ("sla_policy", schema.policy_table(), _flag("PREFLIGHT_REQUIRE_POLICY", "true"))]
    plan.append(("history", schema.work_orders_table(), require_all))
    if schema.holidays_table():
        plan.append(("holidays", schema.holidays_table(), require_all))
    if schema.weather_table():
        plan.append(("weather", schema.weather_table(), require_all))
    checks, blocking, degraded = [], [], []
    for role, table, required in plan:
        c = {"role": role, "required": required, **_count(table, schema.history_count_sql() if role == "history" else None)}
        if role == "history":
            c["table"] = table + " (record_set = history_sample)"
        if role == "history" and c["status"] == "ok":
            want = expected("history_rows")
            c["expected_rows"] = want
            if want is not None and c["rows"] != want:
                c["status"], c["error"] = "partial_load", f"{c['rows']} history rows loaded, {want} expected (data/manifest.json)"
        if role == "work_orders" and c["status"] == "ok":
            want = expected_work_orders()
            c["expected_rows"] = want
            if want is not None and c["rows"] != want:
                c["status"] = "partial_load"
                c["error"] = f"{c['rows']} rows loaded, {want} expected (data/manifest.json)"
        checks.append(c)
        if c["status"] != "ok":
            (blocking if required else degraded).append(role)
    return {"ready": not blocking, "source": data_access.source_name(), "checks": checks, "blocking": blocking,
            "degraded": degraded,
            "message": ("Data confirmed in " + data_access.source_name() if not blocking else
                        "Data not ready: " + ", ".join(f"{c['role']} is {c['status']}" for c in checks if c["role"] in blocking))}


def ensure_ready(tracer=None) -> dict:
    """Gate used by the pipeline before any agent is created. Raises DataNotReadyError (HTTP 503) when not ready."""
    report = run_preflight()
    if tracer:
        tracer.event("Orchestrator", "preflight_data_check", status="success" if report["ready"] else "error",
                     input_summary="SELECT COUNT(1) on " + ", ".join(c["table"] for c in report["checks"]),
                     output_summary=report["message"] + (f"; degraded: {report['degraded']}" if report["degraded"] else ""),
                     checks=report["checks"])
    if not report["ready"]:
        raise DataNotReadyError(report["message"] + ". " + REMEDY, stage="preflight", details=report)
    return report
