"""Derive data/sla_policy.csv from the closed historical work orders (record_set = history_sample).

Method (all assumptions are stated in the output's target_basis column):
  * working-day time to close = Monday-Friday days between initiated and closed date (public holidays not removed here)
  * "responsive" closures = closed within RESPONSIVE_CAP working days. Longer closures in this extract are mostly bulk or
    administrative closures (e.g. a unprioritised order closed 473 days later), which say nothing about a crew's response.
  * target = 75th percentile of responsive closures for the (asset group, priority) cell when it has >= MIN_N of them;
    else the 75th percentile for the whole asset group; else the illustrative fallback below (flagged as such).
  * a higher priority never gets a longer target than a lower one (targets are capped down the 5 > 4 > 3 > 1 ladder).
  * weights per priority level are an ASSUMPTION (5 most urgent), not derived.
Usage: python scripts/derive_sla.py [--csv data/work_orders.csv] [--out data/sla_policy.csv]
"""
import argparse
import csv
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import scoring  # noqa: E402

RESPONSIVE_CAP = 60
MIN_N = 8
LEVELS = (5, 4, 3, 1, 0)
WEIGHTS = {5: 5, 4: 3, 3: 2, 1: 1, 0: 2}
GROUPS = ("Street Light Network", "TOA", "UFA", "AMIS & RMCS")
FALLBACK = {"Street Light Network": {5: 1, 4: 3, 3: 7, 1: 14, 0: 3}, "TOA": {5: 2, 4: 5, 3: 10, 1: 20, 0: 7},
            "UFA": {5: 3, 4: 10, 3: 20, 1: 30, 0: 15}, "AMIS & RMCS": {5: 5, 4: 15, 3: 20, 1: 30, 0: 15}}


p75 = scoring.p75


def closures(rows: list[dict]) -> list[tuple]:
    out = []
    for r in rows:
        if r["record_set"] != "history_sample" or r["is_canceled"] == "Y":
            continue
        a, b = scoring.parse_date(r["initiated_date"]), scoring.parse_date(r["closed_date"])
        if not a or not b or b < a:
            continue
        level = int(float(r["priority"])) if r["priority"] else 0
        out.append((r["asset_group"], level, scoring.working_days(a, b, set())))
    return out


def derive(rows: list[dict]) -> list[dict]:
    data = closures(rows)
    table = []
    for g in GROUPS:
        grp = [d for gg, _, d in data if gg == g]
        grp_resp = [d for d in grp if d <= RESPONSIVE_CAP]
        for lvl in LEVELS:
            cell = [d for gg, ll, d in data if gg == g and ll == lvl]
            resp = [d for d in cell if d <= RESPONSIVE_CAP]
            if len(resp) >= MIN_N:
                target, n = p75(resp), len(resp)
                basis = f"empirical: P75 of {len(resp)} closures within {RESPONSIVE_CAP} working days ({len(cell)} closed in sample)"
            elif len(grp_resp) >= MIN_N:
                target, n = p75(grp_resp), len(grp_resp)
                basis = (f"empirical, asset-group level: P75 of {len(grp_resp)} closures within {RESPONSIVE_CAP} working days "
                         f"({len(grp)} closed in sample); too few for this priority")
            else:
                target, n = FALLBACK[g][lvl], 0
                basis = "illustrative assumption: too few closed orders in the sample"
            table.append({"asset_group": g, "priority_level": lvl, "target_days": target, "weight": WEIGHTS[lvl],
                          "target_basis": basis, "n_obs": n})
    for g in GROUPS:                       # monotone ladder: P5 <= P4 <= P3 <= P1
        rows_g = {r["priority_level"]: r for r in table if r["asset_group"] == g}
        for hi, lo in ((3, 1), (4, 3), (5, 4)):
            if rows_g[hi]["target_days"] > rows_g[lo]["target_days"]:
                rows_g[hi]["target_days"] = rows_g[lo]["target_days"]
                rows_g[hi]["target_basis"] += f"; capped to the P{lo} target"
    return table


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "data" / "work_orders.csv"))
    ap.add_argument("--out", default=str(ROOT / "data" / "sla_policy.csv"))
    args = ap.parse_args()
    with open(args.csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    table = derive(rows)
    with open(args.out, "w", newline="", encoding="utf-8") as g:
        w = csv.DictWriter(g, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)
    for r in table:
        print(f"{r['asset_group']:22} P{r['priority_level']}  target {r['target_days']:>3}d  weight {r['weight']}  n={r['n_obs']:>3}  {r['target_basis'][:60]}")
    print(f"wrote {len(table)} rows to {args.out}")


if __name__ == "__main__":
    main()
