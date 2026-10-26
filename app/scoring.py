"""Deterministic scoring of open work orders: working-day age, SLA overdue, weather allowance, priority score.

SLA targets (derived from history where there is enough of it, else assumed) and weights come from the policy table; not DC policy. Age is counted in working
days: Monday-Friday, minus public holidays (Zetaris REST table). A day counts as weather-impacted when the weather table
says so; the "net of weather" overdue test gives those days back to the crew.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

DEFAULT_TARGET_DAYS = 14
WEATHER_THRESHOLDS = {"precip_mm": 10.0, "wind_max_kmh": 50.0, "temp_max_c": 35.0, "temp_min_c": -5.0}
_DATE = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})")


def parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    m = _DATE.search(str(value))
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def wid(value):
    """Work-order ids arrive as int (Zetaris) or float (SQLite REAL); normalise so they compare and print the same."""
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return value


def to_float(value):
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def holiday_set(rows: list[dict]) -> set[date]:
    return {d for d in (parse_date(r.get("holiday_date")) for r in rows) if d}


def weather_bad_days(rows: list[dict]) -> dict[date, str]:
    """date -> reason, for days that breach any threshold."""
    out = {}
    for r in rows:
        d = parse_date(r.get("weather_date"))
        if not d:
            continue
        p, w = to_float(r.get("precip_mm")), to_float(r.get("wind_max_kmh"))
        tmax, tmin = to_float(r.get("temp_max_c")), to_float(r.get("temp_min_c"))
        why = []
        if p is not None and p >= WEATHER_THRESHOLDS["precip_mm"]:
            why.append("heavy rain")
        if w is not None and w >= WEATHER_THRESHOLDS["wind_max_kmh"]:
            why.append("high wind")
        if tmax is not None and tmax >= WEATHER_THRESHOLDS["temp_max_c"]:
            why.append("extreme heat")
        if tmin is not None and tmin <= WEATHER_THRESHOLDS["temp_min_c"]:
            why.append("hard freeze")
        if why:
            out[d] = " + ".join(why)
    return out


def median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    return float(v[n // 2]) if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def p75(values: list[float]) -> int:
    """75th percentile (nearest rank, rounded up to whole days, at least 1)."""
    import math
    v = sorted(values)
    return max(1, math.ceil(v[int(math.ceil(0.75 * len(v))) - 1]))


def is_working_day(d: date, holidays: set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def working_days(start: date, end: date, holidays: set[date]) -> int:
    """Working days in (start, end]: the day an order is raised does not count."""
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        if is_working_day(d, holidays):
            n += 1
        d += timedelta(days=1)
    return n


def weather_working_days(start: date, end: date, holidays: set[date], bad: dict[date, str]) -> int:
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        if is_working_day(d, holidays) and d in bad:
            n += 1
        d += timedelta(days=1)
    return n


def score_order(row: dict, as_of: date, holidays: set[date], bad: dict[date, str]) -> dict:
    start = parse_date(row.get("initiated_date")) or as_of
    age = working_days(start, as_of, holidays)
    wx = weather_working_days(start, as_of, holidays, bad)
    target = to_float(row.get("target_days"))
    target = int(target) if target is not None and target.is_integer() else target
    policy_missing = target is None
    target = DEFAULT_TARGET_DAYS if policy_missing else target
    weight = to_float(row.get("weight")) or 1.0
    level = int(to_float(row.get("priority_level")) or 0)
    ratio = min(age / target, 3.0) if target > 0 else 3.0
    return {
        "work_order_id": wid(row.get("work_order_id")), "description": row.get("description"),
        "status": row.get("status"), "asset_group": row.get("asset_group"), "ward": row.get("ward"),
        "category": row.get("category"), "priority_level": level, "address": row.get("address"),
        "initiated": start.isoformat(), "age_working_days": age, "weather_days": wx,
        "target_days": target, "target_basis": row.get("target_basis"), "policy_missing": policy_missing, "weight": weight,
        "overdue": age > target, "overdue_net_of_weather": (age - wx) > target,
        "unprioritised": level == 0, "score": round(weight * (1.0 + ratio), 2),
    }


def rank(scored: list[dict]) -> list[dict]:
    return sorted(scored, key=lambda o: (-o["score"], -o["age_working_days"], str(o["work_order_id"])))


def matches(order: dict, filters: list[dict]) -> bool:
    for f in filters:
        dim, member = f["dimension"], str(f["member"])
        value = order.get("priority_level") if dim == "priority_level" else order.get(dim)
        label = "(none)" if value in (None, "") else (str(int(value)) if dim == "priority_level" else str(value))
        if label != member and not (dim == "priority_level" and _same_num(label, member)):
            return False
    return True


def _same_num(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except ValueError:
        return False


RESPONSIVE_CAP = 60      # working days; longer closures are treated as bulk/administrative (see scripts/derive_sla.py)


def history_baseline(rows: list[dict], bad: dict[date, str]) -> dict:
    """Typical time to close from the closed historical sample, plus an exploratory weather comparison.

    Time to close = Monday-Friday days between initiated and closed date, the same measure scripts/derive_sla.py used to
    set the SLA targets. Weather comparison: responsive closures raised on a weather-impacted day versus the rest.
    Descriptive only; `enough_data` is False unless both groups have at least 20 orders.
    """
    items = []
    for r in rows:
        a, b = parse_date(r.get("initiated_date")), parse_date(r.get("closed_date"))
        if a and b and b >= a:
            items.append((str(r.get("asset_group")), a, working_days(a, b, set())))
    groups: dict[str, list[int]] = {}
    for g, _, wd in items:
        groups.setdefault(g, []).append(wd)
    by_group = []
    for g, v in sorted(groups.items()):
        resp = [x for x in v if x <= RESPONSIVE_CAP]
        by_group.append({"asset_group": g, "closed_in_sample": len(v), "closed_within_cap": len(resp),
                         "share_within_cap": round(len(resp) / len(v), 4),
                         "median_working_days_within_cap": median(resp) if resp else None,
                         "p75_working_days_within_cap": p75(resp) if resp else None})
    bad_group = [wd for g, a, wd in items if wd <= RESPONSIVE_CAP and a in bad]
    other = [wd for g, a, wd in items if wd <= RESPONSIVE_CAP and a not in bad]
    weather = {"orders_raised_on_weather_days": len(bad_group), "orders_raised_on_other_days": len(other),
               "median_days_weather": median(bad_group) if bad_group else None,
               "median_days_other": median(other) if other else None,
               "enough_data": len(bad_group) >= 20 and len(other) >= 20}
    firsts = [a for _, a, _ in items]
    return {"closed_in_sample": len(items), "first_raised": min(firsts).isoformat() if firsts else None,
            "last_raised": max(firsts).isoformat() if firsts else None, "responsive_cap_working_days": RESPONSIVE_CAP,
            "by_asset_group": by_group, "weather_effect": weather}
