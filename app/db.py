"""Service-desk persistence: cases, customer conversation, attachments, agent trace, approvals, learned KB."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from app import config

_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE,
    subject TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    customer_email TEXT, customer_name TEXT, customer_id TEXT, order_id TEXT,
    channel TEXT NOT NULL DEFAULT 'portal', goal TEXT, priority TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'New', assignee TEXT DEFAULT 'Unassigned',
    resolution_type TEXT, summary TEXT, resolution TEXT, need_by TEXT, scenario TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, sender TEXT NOT NULL,
    author TEXT NOT NULL, body TEXT NOT NULL, internal INTEGER DEFAULT 0, created_at TEXT NOT NULL,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, filename TEXT, path TEXT,
    content_type TEXT, size INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, plan TEXT NOT NULL,
    reasons TEXT, status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, comment TEXT,
    created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, agent TEXT NOT NULL,
    type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, situation TEXT, resolution TEXT, tags TEXT,
    case_number TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_case ON agent_events(ticket_id, id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec = "seconds")


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents = True, exist_ok = True)
    con = sqlite3.connect(config.APP_DB, timeout = 15, check_same_thread = False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT EXISTS", and a demo
# that crashes on an old data/ directory is worse than a two-line migration.
_ADDED_COLUMNS = (("messages", "meta", "TEXT"),)


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        for table, column, decl in _ADDED_COLUMNS:
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        con.commit()


def query(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def query_one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | list = ()) -> int:
    with _write_lock:
        con = connect()
        try:
            cur = con.execute(sql, params)
            con.commit()
            return cur.lastrowid or 0
        finally:
            con.close()


# --- Cases ------------------------------------------------------------------------

def create_case(**fields: Any) -> dict[str, Any]:
    ts = now_iso()
    fields.setdefault("created_at", ts)
    fields["updated_at"] = ts
    with _write_lock:
        cid = execute(f"INSERT INTO cases ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                      list(fields.values()))
        execute("UPDATE cases SET number = ? WHERE id = ?", (f"CS-{1000 + cid}", cid))
    return get_case(cid)


def get_case(case_id: int) -> dict[str, Any] | None:
    return query_one("SELECT * FROM cases WHERE id = ?", (case_id,))


def update_case(case_id: int, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    execute(f"UPDATE cases SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*fields.values(), case_id])


def list_cases() -> list[dict[str, Any]]:
    return query("SELECT * FROM cases ORDER BY id DESC")


# --- Conversation & attachments -------------------------------------------------------

def add_message(case_id: int, sender: str, author: str, body: str, internal: bool = False,
                meta: dict[str, Any] | None = None) -> int:
    """sender: customer | agent | system.  internal=True -> work note, not visible to the customer.

    `meta` rides along as JSON for anything the UI needs to render beyond the text - currently the
    triage choice chips, so a question with options is one message rather than a message plus a
    parallel store the two could disagree about.
    """
    mid = execute("INSERT INTO messages (case_id, sender, author, body, internal, created_at, meta) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (case_id, sender, author, body, int(internal), now_iso(),
                   json.dumps(meta, default = str) if meta else None))
    update_case(case_id)
    return mid


def messages(case_id: int, include_internal: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM messages WHERE case_id = ?" + ("" if include_internal else " AND internal = 0")
    rows = query(sql + " ORDER BY id", (case_id,))
    for r in rows:
        try:
            r["meta"] = json.loads(r["meta"]) if r.get("meta") else {}
        except (TypeError, ValueError):
            r["meta"] = {}
    return rows


def add_attachment(case_id: int, filename: str, path: str, content_type: str, size: int) -> int:
    return execute("INSERT INTO attachments (case_id, filename, path, content_type, size, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (case_id, filename, path, content_type, size, now_iso()))


def attachments(case_id: int) -> list[dict[str, Any]]:
    return query("SELECT * FROM attachments WHERE case_id = ? ORDER BY id", (case_id,))


# --- Agent events (live trace) -------------------------------------------------------------

def add_event(case_id: int, agent: str, type_: str, payload: dict[str, Any]) -> None:
    execute("INSERT INTO agent_events (ticket_id, agent, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (case_id, agent, type_, json.dumps(payload, default = str), now_iso()))


def events(case_id: int, after_id: int = 0) -> list[dict[str, Any]]:
    rows = query("SELECT * FROM agent_events WHERE ticket_id = ? AND id > ? ORDER BY id", (case_id, after_id))
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


# --- Approvals ------------------------------------------------------------------------

def create_proposal(case_id: int, plan: dict[str, Any], reasons: list[str]) -> int:
    return execute("INSERT INTO proposals (case_id, plan, reasons, created_at) VALUES (?, ?, ?, ?)",
                   (case_id, json.dumps(plan), json.dumps(reasons), now_iso()))


def latest_proposal(case_id: int) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM proposals WHERE case_id = ? ORDER BY id DESC LIMIT 1", (case_id,))
    if row:
        row["plan"], row["reasons"] = json.loads(row["plan"]), json.loads(row["reasons"] or "[]")
    return row


def decide_proposal(pid: int, status: str, by: str, comment: str) -> None:
    execute("UPDATE proposals SET status = ?, decided_by = ?, comment = ?, decided_at = ? WHERE id = ?",
            (status, by, comment, now_iso(), pid))


# --- Learned knowledge ------------------------------------------------------------------

def add_kb(title: str, situation: str, resolution: str, tags: str, case_number: str) -> int:
    return execute("INSERT INTO kb (title, situation, resolution, tags, case_number, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (title, situation, resolution, tags, case_number, now_iso()))


def list_kb() -> list[dict[str, Any]]:
    return query("SELECT * FROM kb ORDER BY id DESC")
