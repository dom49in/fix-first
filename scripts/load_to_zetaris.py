"""Load data/work_orders.csv into Zetaris through its REST API, as views built from inline VALUES.

Zetaris has no writable database here, so each batch of rows becomes one view (work_orders_p01, p02, ...) and a final
view `work_orders` combines them with UNION ALL. The app then queries  <container>.work_orders .

  python scripts/load_to_zetaris.py --dry-run               # write the SQL to scripts/generated/, send nothing
  python scripts/load_to_zetaris.py --probe                 # send ONE row as a test view, then stop
  python scripts/load_to_zetaris.py                         # work orders + SLA policy views, then the two web-API tables, then counts
  python scripts/load_to_zetaris.py --only sla_policy       # just the small policy table (work orders already loaded)
  python scripts/load_to_zetaris.py --register-apis         # just the holidays (Nager.Date) and weather (Open-Meteo) REST tables
  python run.py --load                                      # same thing, from the app entry point; then runs the pre-flight check

Uses the same settings as the app (.env in the project folder): ZETARIS_REST_URL, ZETARIS_ORG_ID, ZETARIS_API_KEY.
If the API refuses CREATE VIEW, use --dry-run and paste scripts/generated/zetaris_load.sql into the Zetaris SQL Editor.
"""
import argparse
import csv
import os
import pathlib
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTAINER = "fixfirst"
COLS = [  # name, SQL type
    ("object_id", "BIGINT"), ("work_order_id", "BIGINT"), ("description", "STRING"), ("status", "STRING"),
    ("initiated_date", "STRING"), ("closed_date", "STRING"), ("actual_start", "STRING"), ("actual_finish", "STRING"),
    ("priority", "INT"), ("category", "STRING"), ("asset_group", "STRING"), ("project_name", "STRING"),
    ("cost", "DOUBLE"), ("is_canceled", "STRING"), ("address", "STRING"), ("ward", "STRING"),
    ("days_to_close", "INT"), ("latitude", "DOUBLE"), ("longitude", "DOUBLE"),
    ("fiscal_year", "INT"), ("record_set", "STRING"),
]


POLICY_COLS = [("asset_group", "STRING"), ("priority_level", "INT"), ("target_days", "INT"), ("weight", "INT"),
               ("target_basis", "STRING"), ("n_obs", "INT")]
MANIFEST = ROOT / "data" / "manifest.json"
API_SQL = ROOT / "scripts" / "zetaris_register_apis.sql"


def load_env() -> list:
    """Read the first .env found in: the project folder, the scripts folder, the current folder. Returns the paths read."""
    found = []
    for folder in (ROOT, ROOT / "scripts", pathlib.Path.cwd()):
        path = folder / ".env"
        if path.is_file() and str(path) not in found:
            found.append(str(path))
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return found


def lit(value: str, sql_type: str) -> str:
    value = (value or "").strip()
    if value == "":
        return "NULL"
    if sql_type in ("BIGINT", "INT"):
        return str(int(float(value)))
    if sql_type == "DOUBLE":
        return repr(float(value))
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def view_sql(name: str, rows: list, cols: list = None) -> str:
    cols = cols or COLS
    values = ",\n".join("(" + ", ".join(lit(r[c], t) for c, t in cols) + ")" for r in rows)
    casts = ", ".join(f"CAST(c{i} AS {t}) AS {c}" for i, (c, t) in enumerate(cols))
    aliases = ", ".join(f"c{i}" for i in range(len(cols)))
    return (f"CREATE SCHEMASTORE VIEW {name} WITH CONTAINER {CONTAINER} AS\n"
            f"SELECT {casts}\nFROM (SELECT * FROM VALUES\n{values}\n) AS t({aliases});")


def build_statements(rows: list, chunk: int) -> list:
    stmts = [f"CREATE SCHEMASTORE CONTAINER {CONTAINER};"]
    parts = []
    for i in range(0, len(rows), chunk):
        name = f"work_orders_p{i // chunk + 1:02d}"
        parts.append(name)
        stmts.append(view_sql(name, rows[i:i + chunk]))
    union = "\nUNION ALL\n".join(f"SELECT * FROM {CONTAINER}.{p}" for p in parts)
    stmts.append(f"CREATE SCHEMASTORE VIEW work_orders WITH CONTAINER {CONTAINER} AS\n{union};")
    return stmts


def policy_statements(rows: list) -> list:
    return [view_sql("sla_policy", rows, POLICY_COLS)]


def api_statements() -> list:
    """Statements from zetaris_register_apis.sql: comment lines removed, split on ';' at end of statement."""
    text = "\n".join(l for l in API_SQL.read_text().splitlines() if not l.strip().startswith("--"))
    return [x.strip() for x in text.split(";") if x.strip()]


def send(sql: str, limit: int = 10) -> dict:
    import requests
    base = os.environ["ZETARIS_REST_URL"].rstrip("/")
    resp = requests.post(
        base + "/sql-editor/sqls/run",
        headers={"Authorization": "Bearer " + os.environ["ZETARIS_API_KEY"], "X-Org-ID": os.environ["ZETARIS_ORG_ID"],
                 "X-Request-ID": str(uuid.uuid4())},
        json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit}, timeout=180)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:300]}
    return {"http": resp.status_code, "body": body}


def count_of(table: str):
    """COUNT(1) of a Zetaris table, or None when it does not exist / cannot be read."""
    try:
        r = send(f"SELECT COUNT(1) AS n FROM {table}")
        if r["http"] < 400:
            return int(float(r["body"]["data"][0][0]))
    except Exception:  # noqa: BLE001
        pass
    return None


def run_steps(label: str, stmts: list, stop_on_fail: bool) -> bool:
    for i, s in enumerate(stmts, 1):
        r = send(s)
        print(f"  [{label} {i}/{len(stmts)}] {s.splitlines()[0][:72]} -> HTTP {r['http']}")
        if r["http"] >= 400 and i > 1:
            print("   ", str(r["body"])[:300])
            if stop_on_fail:
                return False
        time.sleep(0.4)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "data" / "work_orders.csv"))
    ap.add_argument("--chunk", type=int, default=250)
    ap.add_argument("--dry-run", action="store_true", help="write the SQL to scripts/generated/ and send nothing")
    ap.add_argument("--probe", action="store_true", help="send ONE row as a test view, then stop")
    ap.add_argument("--only", choices=["work_orders", "sla_policy"], help="load just one VALUES-based table")
    ap.add_argument("--register-apis", action="store_true", help="only register the holidays and weather REST tables")
    args = ap.parse_args()
    with open(args.csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with open(ROOT / "data" / "sla_policy.csv", newline="", encoding="utf-8") as f:
        policy = list(csv.DictReader(f))
    hist = sum(1 for r in rows if r["record_set"] == "history_sample")
    MANIFEST.write_text(__import__("json").dumps({"work_orders_rows": len(rows), "active_rows": len(rows) - hist,
                                                  "history_rows": hist, "sla_policy_rows": len(policy)}))
    print(f"{len(rows)} work orders ({len(rows) - hist} active + {hist} history) and {len(policy)} SLA policy rows read")
    if args.dry_run:
        out = ROOT / "scripts" / "generated"
        out.mkdir(exist_ok=True)
        stmts = build_statements(rows, args.chunk) + policy_statements(policy)
        (out / "zetaris_load.sql").write_text("\n\n".join(stmts) + "\n")
        print(f"wrote {len(stmts)} statements ({sum(len(s) for s in stmts):,} chars) to {out / 'zetaris_load.sql'}")
        print("web-API tables: paste scripts/zetaris_register_apis.sql into the SQL Editor")
        return 0
    found = load_env()
    needed = ("ZETARIS_REST_URL", "ZETARIS_ORG_ID", "ZETARIS_API_KEY")
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        print(f"Missing settings: {', '.join(missing)}")
        print("Looked for .env in:", ", ".join(str(f / '.env') for f in (ROOT, ROOT / 'scripts', pathlib.Path.cwd())))
        print("Found:", found or "no .env file")
        stray = [p.name for p in ROOT.iterdir() if p.name.lower().startswith(".env") or p.name.lower().startswith("env")]
        print("Files in the project folder that look like env files:", stray or "none")
        print("Present keys (names only):", sorted(k for k in os.environ if k.startswith("ZETARIS")) or "none")
        return 2
    print("Settings found in:", found or "environment variables")
    if args.probe:
        sql = view_sql("work_orders_probe", rows[:1])
        print(send(f"CREATE SCHEMASTORE CONTAINER {CONTAINER};"))
        print(send(sql)); print(send(f"SELECT COUNT(*) AS n FROM {CONTAINER}.work_orders_probe"))
        return 0
    todo = {"work_orders": not args.register_apis and args.only in (None, "work_orders"),
            "sla_policy": not args.register_apis and args.only in (None, "sla_policy"),
            "apis": args.register_apis or args.only is None}
    tables = {"work_orders": (f"{CONTAINER}.work_orders", len(rows)), "sla_policy": (f"{CONTAINER}.sla_policy", len(policy))}
    ok = True
    for name in ("work_orders", "sla_policy"):
        if not todo[name]:
            continue
        table, want = tables[name]
        have = count_of(table)
        if have == want:
            print(f"{name}: already loaded ({have} rows), skipped")
            continue
        if have:
            print(f"{name}: {have} rows found but {want} expected. Drop the views in the SQL Editor "
                  f"(DROP VIEW {table}; and its _pNN batches), then run again.")
            ok = False
            continue
        print(f"{name}: loading")
        stmts = build_statements(rows, args.chunk) if name == "work_orders" else \
            [f"CREATE SCHEMASTORE CONTAINER {CONTAINER};"] + policy_statements(policy)
        if not run_steps(name, stmts, stop_on_fail=True):
            print("Stopped at the first failure. Use --dry-run and paste scripts/generated/zetaris_load.sql into the SQL Editor.")
            return 1
    if todo["apis"]:
        for table, label in ((f"{CONTAINER}.us_holidays", "holidays"), (f"{CONTAINER}.weather_daily", "weather")):
            if count_of(table):
                print(f"{label}: already registered, skipped")
                continue
            part = [x for x in api_statements() if (label == "holidays") == ("holidays" in x.lower() or x.lower().startswith("create lightning database"))
                    and not x.lower().startswith("select count")]
            if label == "weather":
                part = [x for x in part if "weather" in x.lower()]
            print(f"{label}: registering REST table (Zetaris web API; unverified endpoint, failures are non-fatal)")
            if not run_steps(label, ["CREATE SCHEMASTORE CONTAINER " + CONTAINER] + part if label == "holidays" else part, stop_on_fail=False):
                ok = False
    print("\nCounts in Zetaris:")
    for t in (f"{CONTAINER}.work_orders", f"{CONTAINER}.sla_policy", f"{CONTAINER}.us_holidays", f"{CONTAINER}.weather_daily"):
        print(f"  {t}: {count_of(t)}")
    print("Next: python run.py --preflight   (the agent runs only after this passes)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
