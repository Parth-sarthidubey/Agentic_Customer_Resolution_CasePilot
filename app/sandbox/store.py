"""The simulated enterprise: one SQLite database standing in for CRM, OMS, WMS,
payment gateway and carrier systems. Every state change is written to an audit log."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from app import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT, email TEXT UNIQUE, tier TEXT, joined TEXT,
    lifetime_value REAL, claims_90d INTEGER DEFAULT 0, last_goodwill_at TEXT
);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY, name TEXT, category TEXT, price REAL, final_sale INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS warehouses (id TEXT PRIMARY KEY, name TEXT, region TEXT);
CREATE TABLE IF NOT EXISTS transit_days (warehouse_id TEXT, region TEXT, days INTEGER, PRIMARY KEY (warehouse_id, region));
CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT, warehouse_id TEXT, on_hand INTEGER, reserved INTEGER DEFAULT 0, PRIMARY KEY (sku, warehouse_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, customer_id TEXT, status TEXT, placed_at TEXT, shipped_at TEXT, delivered_at TEXT,
    region TEXT, total REAL, payment_id TEXT
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id TEXT, sku TEXT, qty INTEGER, unit_price REAL, status TEXT DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, order_id TEXT, method TEXT, captured REAL, refunded REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY, payment_id TEXT, order_id TEXT, amount REAL, reason TEXT, status TEXT,
    idempotency_key TEXT UNIQUE, created_at TEXT
);
CREATE TABLE IF NOT EXISTS shipments (
    id TEXT PRIMARY KEY, order_id TEXT, kind TEXT, carrier TEXT, status TEXT, warehouse_id TEXT,
    sku TEXT, eta TEXT, delivered_at TEXT, proof TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS shipment_events (shipment_id TEXT, ts TEXT, status TEXT, location TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS store_credit (
    id TEXT PRIMARY KEY, customer_id TEXT, amount REAL, reason TEXT, case_ref TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, case_ref TEXT, body TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS world_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT, effect TEXT, description TEXT,
    fired INTEGER DEFAULT 0, fired_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, system TEXT, action TEXT, detail TEXT, kind TEXT DEFAULT 'change'
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec = "seconds")


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents = True, exist_ok = True)
    con = sqlite3.connect(config.STORE_DB, timeout = 15, check_same_thread = False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)


def q(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def q1(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql: str, params: tuple | list = ()) -> int:
    with _lock:
        con = connect()
        try:
            cur = con.execute(sql, params)
            con.commit()
            return cur.lastrowid or 0
        finally:
            con.close()


def audit(system: str, action: str, detail: dict[str, Any] | str, kind: str = "change") -> None:
    x("INSERT INTO audit (ts, system, action, detail, kind) VALUES (?, ?, ?, ?, ?)",
      (now_iso(), system, action, detail if isinstance(detail, str) else json.dumps(detail), kind))


def next_id(prefix: str, table: str) -> str:
    """Next free id for a generated record, e.g. SHP-9007.

    Derived from the highest existing suffix rather than a row count: restoring a single
    scenario deletes rows, and a count-based id would then walk backwards and collide with
    records that are still there. Generated ids start above the seeded ones.
    """
    row = q1(f"SELECT MAX(CAST(SUBSTR(id, ?) AS INTEGER)) AS m FROM {table} WHERE id LIKE ?",
             (len(prefix) + 2, f"{prefix}-%"))
    return f"{prefix}-{max((row or {}).get('m') or 0, 9000) + 1}"
