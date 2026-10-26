"""SAMPLE_MODE data source: the same tables, loaded from local CSVs into in-memory SQLite.

work_orders is the REAL DC Cityworks extract (data/work_orders.csv). sla_policy is derived from the historical rows (scripts/derive_sla.py).
us_holidays and weather_daily stand in for the two web-API tables Zetaris serves in live mode; the weather file is
SYNTHETIC and exists only so the offline path and the tests can run. Nothing here returns a prewritten answer.
"""
from __future__ import annotations

import csv
import sqlite3
import threading
from pathlib import Path

from . import config, schema
from .errors import DataError
from .sqlguard import assert_read_only

DATA = config.ROOT / "data"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_key: tuple | None = None


def _sources() -> dict[str, Path]:
    out = {schema.work_orders_table(): Path(config.get("SAMPLE_DATA_PATH", str(DATA / "work_orders.csv"))),
           schema.policy_table(): DATA / "sla_policy.csv"}
    if schema.holidays_table():
        out[schema.holidays_table()] = DATA / "us_holidays_sample.csv"
    if schema.weather_table():
        out[schema.weather_table()] = DATA / "weather_synthetic_for_tests.csv"
    return out


def _load(conn: sqlite3.Connection, path: Path, table: str) -> None:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows, cols = list(reader), reader.fieldnames or []
    numeric = set()
    for c in cols:
        vals = [r[c] for r in rows if r[c] != ""]
        try:
            for v in vals:
                float(v)
            if vals:
                numeric.add(c)
        except ValueError:
            pass
    defs = ", ".join(f"{c} {'REAL' if c in numeric else 'TEXT'}" for c in cols)
    conn.execute(f"CREATE TABLE {table} ({defs})")
    conn.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in cols)})",
                     [[None if r[c] == "" else (float(r[c]) if c in numeric else r[c]) for c in cols] for r in rows])


def _connect() -> sqlite3.Connection:
    global _conn, _key
    sources = _sources()
    key = tuple(sorted((k, str(v)) for k, v in sources.items()))
    if _conn is not None and _key == key:
        return _conn
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    for name in sorted({t.split(".")[0] for t in sources if "." in t}):
        conn.execute(f"ATTACH DATABASE ':memory:' AS {name}")
    for table, path in sources.items():
        if not path.exists():
            raise DataError(f"Sample data not found at {path}", stage="sample_data")
        _load(conn, path, table)
    conn.commit()
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    _conn, _key = conn, key
    return conn


def run_sql(sql: str) -> list[dict]:
    assert_read_only(sql)
    with _lock:
        try:
            cur = _connect().execute(sql)
            return [{k.lower(): row[k] for k in row.keys()} for row in cur.fetchall()]
        except sqlite3.Error as exc:
            raise DataError(f"Sample data query failed: {exc}", stage="sample_data") from exc
