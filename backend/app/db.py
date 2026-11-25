from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL,
  name TEXT NOT NULL,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  UNIQUE(business_day, name)
);
CREATE TABLE IF NOT EXISTS chutes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  line_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '口门开',
  soft_cap INTEGER NOT NULL DEFAULT 200,
  UNIQUE(line_id, code),
  FOREIGN KEY(line_id) REFERENCES lines(id)
);
CREATE TABLE IF NOT EXISTS route_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dest_code TEXT NOT NULL UNIQUE,
  chute_id INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  barcode TEXT NOT NULL,
  dest_code TEXT NOT NULL DEFAULT '',
  shift_id INTEGER NOT NULL,
  line_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '待路由',
  chute_id INTEGER,
  bag_id INTEGER,
  divert_batch_id INTEGER,
  scanned_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(line_id) REFERENCES lines(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS route_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(scan_id) REFERENCES scans(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS divert_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '回流中',
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id)
);
CREATE TABLE IF NOT EXISTS bags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '集包中',
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref_id INTEGER,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- 调度屏四格真源：装载 / 裁决 / 回流 / 开袋 / 入袋 / 封袋 的只追加事件流。
-- 四格、口门在口、袋册、扫描册、审计重放一律由本表重放投影得到，禁止另立可变计数。
CREATE TABLE IF NOT EXISTS sort_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  scan_id INTEGER,
  bag_id INTEGER,
  batch_id INTEGER,
  chute_id INTEGER,
  note TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '系统',
  dedup_key TEXT UNIQUE,
  created_at TEXT NOT NULL,
  FOREIGN KEY(shift_id) REFERENCES shifts(id)
);
CREATE INDEX IF NOT EXISTS idx_sort_events_scan ON sort_events(scan_id, id);
CREATE INDEX IF NOT EXISTS idx_sort_events_bag ON sort_events(bag_id, id);
CREATE INDEX IF NOT EXISTS idx_sort_events_shift ON sort_events(shift_id, id);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx():
    """写口事务：BEGIN IMMEDIATE 一开始就抢写锁，装载与封袋并发不会读到中间态。

    事件追加、缓存重算、审计落库必须同处一个事务；任一步失败整笔回滚，
    不留半截回执（四格与点计不会出现一格加一格减的漂数）。
    """
    conn = connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def read_tx():
    """读快照事务：在 WAL 下让身份表与事件流处于同一快照，重放不会跨交易。"""
    conn = connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]
