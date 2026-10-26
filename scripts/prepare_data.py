"""Prepare data/work_orders.csv from the DC Cityworks work-order export.

Source: Cityworks Workorders, District of Columbia (https://catalog.data.gov/dataset/cityworks-workorders),
licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0). Changes made here: columns selected and renamed,
columns that name individual city staff and columns that are empty dropped, nothing else altered.

Two inputs are merged into ONE table, each row tagged with where it came from:
  --active   the current extract (FY2026), record_set = active_fy2026
  --history  a sample of closed historical work orders (FY2023-2024), record_set = history_sample
The agent triages only OPEN work orders (all active); the history rows feed the SLA targets and the closing-time baseline.

Usage: python scripts/prepare_data.py --active ACTIVE_EXPORT.csv --history HISTORY_EXPORT.csv [--out data/work_orders.csv]
"""
import argparse
import csv
import pathlib

KEEP = {  # source column -> output column
    "OBJECTID": "object_id", "WORKORDERID": "work_order_id", "DESCRIPTION": "description", "STATUS": "status",
    "INITIATEDDATE": "initiated_date", "WORKORDERCLOSEDDATE": "closed_date", "ACTUALSTARTDATE": "actual_start",
    "ACTUALFINISHDATE": "actual_finish", "PRIORITY": "priority", "WORKORDERCATEGORY": "category",
    "ASSETGROUP": "asset_group", "PROJECTNAME": "project_name", "WORKORDERCOST": "cost", "ISCANCELED": "is_canceled",
    "ADDRESS": "address", "WARD": "ward", "DAYSTOCLOSE": "days_to_close", "LATITUDE": "latitude", "LONGITUDE": "longitude",
    "FISCALYEAR": "fiscal_year",
}


def read(path: str, record_set: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in KEEP if c not in reader.fieldnames]
        if missing:
            raise SystemExit(f"{path} is missing columns: {missing}")
        return [{**{new: (row[old] or "").strip() for old, new in KEEP.items()}, "record_set": record_set} for row in reader]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--active", required=True)
    ap.add_argument("--history", required=True)
    ap.add_argument("--out", default="data/work_orders.csv")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = read(args.active, "active_fy2026")
    hist = read(args.history, "history_sample")
    seen = {r["work_order_id"] for r in rows}
    dup = [r for r in hist if r["work_order_id"] in seen]
    hist = [r for r in hist if r["work_order_id"] not in seen]
    with open(out, "w", newline="", encoding="utf-8") as g:
        writer = csv.DictWriter(g, fieldnames=[*KEEP.values(), "record_set"])
        writer.writeheader()
        writer.writerows(rows + hist)
    print(f"wrote {len(rows)} active + {len(hist)} history rows to {out} ({len(dup)} duplicate ids skipped)")


if __name__ == "__main__":
    main()
