"""Table names, the dimension whitelist and every SQL statement the agents may run.

The model never writes SQL. It only picks a dimension from DIMENSIONS and a member from a list the app has just
queried; the builders below turn that into SQL. Everything is plain SELECT / JOIN / GROUP BY so the same text runs on
Zetaris and on the SQLite used by SAMPLE_MODE. Date arithmetic is deliberately NOT done in SQL (dialects differ):
dates are returned as text and aged in app/scoring.py.
"""
from __future__ import annotations

from . import config
from .errors import RequestError

# Statuses that count as open work. COMPLETE, CANCEL, CLOSED, NWN and INCOMPLETE are treated as done.
OPEN_STATUSES = ("OPEN", "01-ASSIGNED", "02-INPROGRESS", "PENDING")

# dimension -> SQL expression (w = work order, p = SLA policy row)
DIM_SQL = {
    "asset_group": "COALESCE(w.asset_group, '(none)')",
    "ward": "COALESCE(w.ward, '(none)')",
    "category": "COALESCE(w.category, '(none)')",
    "priority_level": "COALESCE(w.priority, 0)",
    "status": "COALESCE(w.status, '(none)')",
}
DIMENSIONS = ["asset_group", "ward", "category", "priority_level"]   # probed by the Markovian loop
FILTER_DIMENSIONS = list(DIM_SQL)                                       # also valid as a scope filter (adds status)
INT_DIMS = {"priority_level"}
MAX_BASE_FILTERS = 2

# words that signal the maintenance domain to H-MEM's domain score
DOMAIN_ALIASES = {"backlog": ("backlog", "open", "overdue", "priority", "crew", "crews", "fix", "first", "triage",
                              "maintenance", "work", "orders", "ward", "tree", "light", "lights", "street", "sla")}
MEASURE = "backlog"        # the memory/engine "metric" name


def work_orders_table() -> str:
    return config.get("ZETARIS_TABLE", "fixfirst.work_orders")


def policy_table() -> str:
    return config.get("ZETARIS_POLICY_TABLE", "fixfirst.sla_policy")


def holidays_table() -> str | None:
    """Second source: US public holidays from the Nager.Date web API, registered in Zetaris as a REST table. Empty = off."""
    value = config.get("ZETARIS_HOLIDAY_TABLE", "fixfirst.us_holidays")
    return None if value.lower() in ("none", "off", "false") else value


def _ident(name: str) -> str:
    if not all(c.isalnum() or c in "._" for c in name):
        raise RequestError(f"Unsafe table name {name!r}.", stage="schema")
    return name


def _lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _from() -> str:
    return (f"FROM {_ident(work_orders_table())} w LEFT JOIN {_ident(policy_table())} p "
            f"ON p.asset_group = w.asset_group AND p.priority_level = COALESCE(w.priority, 0)")


def _open_where() -> str:
    return "w.status IN (" + ", ".join(_lit(s) for s in OPEN_STATUSES) + ")"


def filter_sql(filters: list[dict]) -> str:
    parts = []
    for f in filters:
        dim, member = f["dimension"], f["member"]
        if dim not in DIM_SQL:
            raise RequestError(f"Unknown dimension {dim!r}.", stage="schema")
        if dim in INT_DIMS:
            try:
                parts.append(f"{DIM_SQL[dim]} = {int(float(member))}")
            except (TypeError, ValueError) as exc:
                raise RequestError(f"{dim} member must be a number, got {member!r}.", stage="schema") from exc
        elif member == "(none)":
            parts.append(f"{DIM_SQL[dim].split(',')[0].replace('COALESCE(', '')} IS NULL")
        else:
            parts.append(f"{DIM_SQL[dim].split(',')[0].replace('COALESCE(', '')} = {_lit(member)}")
    return "".join(" AND " + p for p in parts)


# -- statements ----------------------------------------------------------------------------------------------------
def count_sql(table: str) -> str:
    return f"SELECT COUNT(1) AS n FROM {_ident(table)}"


ACTIVE_SET = "active_fy2026"
HISTORY_SET = "history_sample"


def as_of_sql() -> str:
    """The snapshot date is the last work order raised in the ACTIVE extract (history rows are older and closed)."""
    return (f"SELECT MAX(initiated_date) AS as_of, MIN(initiated_date) AS first_day, COUNT(1) AS n "
            f"FROM {_ident(work_orders_table())} WHERE record_set = '{ACTIVE_SET}'")


def history_count_sql() -> str:
    return f"SELECT COUNT(1) AS n FROM {_ident(work_orders_table())} WHERE record_set = '{HISTORY_SET}'"


def history_sql() -> str:
    """Closed, not-cancelled historical work orders: the baseline for typical time to close."""
    return ("SELECT w.asset_group AS asset_group, COALESCE(w.priority, 0) AS priority_level, "
            "w.initiated_date AS initiated_date, w.closed_date AS closed_date "
            f"FROM {_ident(work_orders_table())} w WHERE w.record_set = '{HISTORY_SET}' AND w.closed_date IS NOT NULL "
            "AND COALESCE(w.is_canceled, 'N') <> 'Y' ORDER BY w.work_order_id")


HISTORY_COLUMNS = ["asset_group", "priority_level", "initiated_date", "closed_date"]


def total_sql(filters: list[dict]) -> str:
    return (f"SELECT COUNT(1) AS n_orders, SUM(COALESCE(p.weight, 1)) AS points {_from()} "
            f"WHERE {_open_where()}{filter_sql(filters)}")


TOTAL_COLUMNS = ["n_orders", "points"]


def breakdown_sql(dimension: str, filters: list[dict]) -> str:
    if dimension not in DIM_SQL:
        raise RequestError(f"Unknown dimension {dimension!r}.", stage="schema")
    expr = DIM_SQL[dimension]
    return (f"SELECT {expr} AS member, COUNT(1) AS n_orders, SUM(COALESCE(p.weight, 1)) AS points {_from()} "
            f"WHERE {_open_where()}{filter_sql(filters)} GROUP BY {expr}")


BREAKDOWN_COLUMNS = ["member", "n_orders", "points"]

WORKSET_COLUMNS = ["work_order_id", "description", "status", "initiated_date", "priority_level", "category",
                   "asset_group", "ward", "address", "latitude", "longitude", "target_days", "weight", "target_basis"]


def workset_sql(filters: list[dict]) -> str:
    return ("SELECT w.work_order_id AS work_order_id, w.description AS description, w.status AS status, "
            "w.initiated_date AS initiated_date, COALESCE(w.priority, 0) AS priority_level, w.category AS category, "
            "w.asset_group AS asset_group, w.ward AS ward, w.address AS address, w.latitude AS latitude, "
            "w.longitude AS longitude, p.target_days AS target_days, COALESCE(p.weight, 1) AS weight, "
            "p.target_basis AS target_basis "
            f"{_from()} WHERE {_open_where()}{filter_sql(filters)} ORDER BY w.work_order_id")


def holidays_sql() -> str:
    return f"SELECT holiday_date, holiday_name FROM {_ident(holidays_table() or '')} ORDER BY holiday_date"


HOLIDAY_COLUMNS = ["holiday_date", "holiday_name"]


def weather_table() -> str | None:
    """Third source: daily weather (Open-Meteo archive API) registered in Zetaris as a REST table. Empty = off."""
    value = config.get("ZETARIS_WEATHER_TABLE", "fixfirst.weather_daily")
    return None if value.lower() in ("none", "off", "false") else value


def weather_sql() -> str:
    return (f"SELECT weather_date, precip_mm, wind_max_kmh, temp_max_c, temp_min_c FROM {_ident(weather_table() or '')} "
            f"ORDER BY weather_date")


WEATHER_COLUMNS = ["weather_date", "precip_mm", "wind_max_kmh", "temp_max_c", "temp_min_c"]
