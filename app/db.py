from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "app.db"
SEED_PATH = DATA_DIR / "seed_data.json"

STORE_IDS = {
    "13972005974": "金力",
    "18671708514": "江升",
}

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin', 'viewer'))
);

CREATE TABLE IF NOT EXISTS months (
  ym TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'ready')),
  notes TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ym TEXT NOT NULL,
  store TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('retail', 'purchase', 'cabinet')),
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  uploaded_by TEXT,
  uploaded_at TEXT,
  FOREIGN KEY (ym) REFERENCES months(ym) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS retail_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ym TEXT NOT NULL,
  store TEXT NOT NULL,
  barcode TEXT,
  code TEXT,
  name TEXT NOT NULL,
  category TEXT,
  qty REAL DEFAULT 0,
  amount REAL DEFAULT 0,
  cost REAL DEFAULT 0,
  profit REAL DEFAULT 0,
  margin REAL
);

CREATE TABLE IF NOT EXISTS purchase_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ym TEXT NOT NULL,
  store TEXT NOT NULL,
  date TEXT,
  supplier TEXT,
  name TEXT,
  category TEXT,
  qty REAL DEFAULT 0,
  amount REAL DEFAULT 0,
  biz_type TEXT
);

CREATE TABLE IF NOT EXISTS cabinet_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ym TEXT NOT NULL,
  store TEXT NOT NULL DEFAULT '金力',
  name TEXT,
  category TEXT,
  qty REAL DEFAULT 0,
  retail_amt REAL DEFAULT 0,
  cost_amt REAL DEFAULT 0,
  point TEXT,
  is_transfer INTEGER DEFAULT 0,
  date TEXT
);

CREATE TABLE IF NOT EXISTS report_cache (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  payload TEXT NOT NULL,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_retail_ym_store ON retail_lines(ym, store);
CREATE INDEX IF NOT EXISTS idx_purchase_ym_store ON purchase_lines(ym, store);
CREATE INDEX IF NOT EXISTS idx_cabinet_ym ON cabinet_lines(ym);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin', 'viewer'))
);

CREATE TABLE IF NOT EXISTS months (
  ym TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'ready')),
  notes TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS uploads (
  id SERIAL PRIMARY KEY,
  ym TEXT NOT NULL REFERENCES months(ym) ON DELETE CASCADE,
  store TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('retail', 'purchase', 'cabinet')),
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  uploaded_by TEXT,
  uploaded_at TEXT
);

CREATE TABLE IF NOT EXISTS retail_lines (
  id SERIAL PRIMARY KEY,
  ym TEXT NOT NULL,
  store TEXT NOT NULL,
  barcode TEXT,
  code TEXT,
  name TEXT NOT NULL,
  category TEXT,
  qty DOUBLE PRECISION DEFAULT 0,
  amount DOUBLE PRECISION DEFAULT 0,
  cost DOUBLE PRECISION DEFAULT 0,
  profit DOUBLE PRECISION DEFAULT 0,
  margin DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS purchase_lines (
  id SERIAL PRIMARY KEY,
  ym TEXT NOT NULL,
  store TEXT NOT NULL,
  date TEXT,
  supplier TEXT,
  name TEXT,
  category TEXT,
  qty DOUBLE PRECISION DEFAULT 0,
  amount DOUBLE PRECISION DEFAULT 0,
  biz_type TEXT
);

CREATE TABLE IF NOT EXISTS cabinet_lines (
  id SERIAL PRIMARY KEY,
  ym TEXT NOT NULL,
  store TEXT NOT NULL DEFAULT '金力',
  name TEXT,
  category TEXT,
  qty DOUBLE PRECISION DEFAULT 0,
  retail_amt DOUBLE PRECISION DEFAULT 0,
  cost_amt DOUBLE PRECISION DEFAULT 0,
  point TEXT,
  is_transfer INTEGER DEFAULT 0,
  date TEXT
);

CREATE TABLE IF NOT EXISTS report_cache (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  payload TEXT NOT NULL,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_retail_ym_store ON retail_lines(ym, store);
CREATE INDEX IF NOT EXISTS idx_purchase_ym_store ON purchase_lines(ym, store);
CREATE INDEX IF NOT EXISTS idx_cabinet_ym ON cabinet_lines(ym);
"""


def using_postgres() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _qmark_to_percent(sql: str) -> str:
    """把 SQLite 风格 ? 占位符转为 psycopg %s（忽略字符串内的问号——本项目 SQL 无此情况）。"""
    return sql.replace("?", "%s")


class _PgCursor:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        return row

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PgConn:
    """尽量兼容 sqlite3.Connection 的常用用法。"""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params: Sequence[Any] | None = None):
        cur = self._raw.cursor()
        if params is None:
            cur.execute(_qmark_to_percent(sql))
        else:
            cur.execute(_qmark_to_percent(sql), params)
        return _PgCursor(cur)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        cur = self._raw.cursor()
        cur.executemany(_qmark_to_percent(sql), list(seq_of_params))
        return _PgCursor(cur)

    def executescript(self, script: str):
        # 按分号拆分；忽略空语句
        for stmt in _split_sql(script):
            self._raw.execute(stmt)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self._raw.close()
        return False


class _SqliteConn:
    """包装 sqlite3，with 块结束时 commit（与原先 check_same_thread 用法一致）。"""

    def __init__(self, raw: sqlite3.Connection):
        self._raw = raw

    def execute(self, sql: str, params: Sequence[Any] | None = None):
        if params is None:
            return self._raw.execute(sql)
        return self._raw.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]):
        return self._raw.executemany(sql, seq_of_params)

    def executescript(self, script: str):
        return self._raw.executescript(script)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self._raw.close()
        return False


def _split_sql(script: str) -> list[str]:
    parts = []
    buf = []
    for line in script.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        if s.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                parts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        parts.append(tail)
    return parts


def connect():
    ensure_dirs()
    url = os.getenv("DATABASE_URL")
    if url:
        import psycopg
        from psycopg.rows import dict_row

        # Render/Neon 有时给 postgres://
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        raw = psycopg.connect(url, row_factory=dict_row)
        return _PgConn(raw)

    raw = sqlite3.connect(DB_PATH, check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return _SqliteConn(raw)


def init_db() -> None:
    ensure_dirs()
    schema = SCHEMA_POSTGRES if using_postgres() else SCHEMA_SQLITE
    with connect() as conn:
        conn.executescript(schema)
    migrate_db()
    load_seed_if_empty()


def migrate_db() -> None:
    """幂等迁移：为已有库补齐 cabinet_lines.store。"""
    with connect() as conn:
        if using_postgres():
            conn.execute(
                "ALTER TABLE cabinet_lines ADD COLUMN IF NOT EXISTS store TEXT NOT NULL DEFAULT '金力'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cabinet_ym_store ON cabinet_lines(ym, store)"
            )
            return

        cols = [r["name"] for r in conn.execute("PRAGMA table_info(cabinet_lines)").fetchall()]
        if "store" not in cols:
            conn.execute(
                "ALTER TABLE cabinet_lines ADD COLUMN store TEXT NOT NULL DEFAULT '金力'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cabinet_ym_store ON cabinet_lines(ym, store)"
        )


def _table_count(conn, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    if row is None:
        return 0
    # sqlite Row / dict_row
    try:
        return int(row["c"])
    except Exception:
        return int(row[0])


def load_seed_if_empty() -> bool:
    """业务表为空时导入 seed_data.json（不含 users）。"""
    if not SEED_PATH.exists():
        return False
    with connect() as conn:
        if _table_count(conn, "months") > 0:
            return False
        data = json.loads(SEED_PATH.read_text(encoding="utf-8"))

        # 顺序：months → 明细 → uploads → cache
        for row in data.get("months", []):
            conn.execute(
                "INSERT INTO months (ym, status, notes, updated_at) VALUES (?, ?, ?, ?)",
                (row["ym"], row["status"], row.get("notes"), row.get("updated_at")),
            )
        for row in data.get("retail_lines", []):
            conn.execute(
                """
                INSERT INTO retail_lines
                (ym, store, barcode, code, name, category, qty, amount, cost, profit, margin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["ym"],
                    row["store"],
                    row.get("barcode"),
                    row.get("code"),
                    row["name"],
                    row.get("category"),
                    row.get("qty") or 0,
                    row.get("amount") or 0,
                    row.get("cost") or 0,
                    row.get("profit") or 0,
                    row.get("margin"),
                ),
            )
        for row in data.get("purchase_lines", []):
            conn.execute(
                """
                INSERT INTO purchase_lines
                (ym, store, date, supplier, name, category, qty, amount, biz_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["ym"],
                    row["store"],
                    row.get("date"),
                    row.get("supplier"),
                    row.get("name"),
                    row.get("category"),
                    row.get("qty") or 0,
                    row.get("amount") or 0,
                    row.get("biz_type"),
                ),
            )
        for row in data.get("cabinet_lines", []):
            conn.execute(
                """
                INSERT INTO cabinet_lines
                (ym, store, name, category, qty, retail_amt, cost_amt, point, is_transfer, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["ym"],
                    row.get("store") or "金力",
                    row.get("name"),
                    row.get("category"),
                    row.get("qty") or 0,
                    row.get("retail_amt") or 0,
                    row.get("cost_amt") or 0,
                    row.get("point"),
                    row.get("is_transfer") or 0,
                    row.get("date"),
                ),
            )
        for row in data.get("uploads", []):
            conn.execute(
                """
                INSERT INTO uploads (ym, store, kind, filename, path, uploaded_by, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["ym"],
                    row["store"],
                    row["kind"],
                    row["filename"],
                    row.get("path") or "",
                    row.get("uploaded_by"),
                    row.get("uploaded_at"),
                ),
            )
        for row in data.get("report_cache", []):
            conn.execute(
                """
                INSERT INTO report_cache (id, payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (row["payload"], row.get("updated_at")),
            )
    return True
