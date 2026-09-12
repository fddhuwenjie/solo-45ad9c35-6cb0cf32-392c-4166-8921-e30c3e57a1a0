"""SQLite 存储层：演出、场次、造型、服装、任务与修订。"""
import json
import os
import sqlite3
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "quickchange.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS productions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scenes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  name TEXT NOT NULL,
  start_sec INTEGER NOT NULL,
  duration_sec INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS actors(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  code TEXT NOT NULL DEFAULT '',
  default_side TEXT NOT NULL DEFAULT 'L'
);
CREATE TABLE IF NOT EXISTS looks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  actor_id INTEGER NOT NULL,
  scene_id INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'costume',      -- costume | shoes | prop
  layer INTEGER NOT NULL DEFAULT 1,          -- 1=最内层，数字越大越靠外
  don_sec INTEGER NOT NULL DEFAULT 10,
  doff_sec INTEGER NOT NULL DEFAULT 8,
  status TEXT NOT NULL DEFAULT 'ok',         -- ok | cleaning | repair
  available_at INTEGER NOT NULL DEFAULT 0,   -- 清洁/维修后可用的演出时刻(秒)
  cart_id INTEGER,
  copies INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS look_items(
  look_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL,
  ord INTEGER NOT NULL DEFAULT 0,
  UNIQUE(look_id, item_id)
);
CREATE TABLE IF NOT EXISTS dressers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  side TEXT NOT NULL DEFAULT 'L',
  x REAL NOT NULL DEFAULT 5,
  y REAL NOT NULL DEFAULT 5,
  capacity INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS carts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  side TEXT NOT NULL DEFAULT 'L',
  x REAL NOT NULL DEFAULT 5,
  y REAL NOT NULL DEFAULT 15,
  capacity INTEGER NOT NULL DEFAULT 20
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  actor_id INTEGER NOT NULL,
  from_scene_id INTEGER NOT NULL,
  to_scene_id INTEGER NOT NULL,
  exit_side TEXT NOT NULL DEFAULT 'L',
  position_id INTEGER,
  dresser_id INTEGER,
  start_sec INTEGER,            -- 计划开始（拖动/重排结果）；NULL=自动
  locked INTEGER NOT NULL DEFAULT 0,
  needs_review INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS revisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  created_at REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  snapshot TEXT NOT NULL        -- JSON: {scenes,tasks,items}
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    con = connect()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def row(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def load_state(production_id=1):
    """读取排程所需的全部数据。"""
    con = connect()
    try:
        pid = production_id
        state = {
            "production": row(con, "SELECT * FROM productions WHERE id=?", (pid,)),
            "scenes": rows(con, "SELECT * FROM scenes WHERE production_id=? ORDER BY seq", (pid,)),
            "actors": rows(con, "SELECT * FROM actors WHERE production_id=? ORDER BY id", (pid,)),
            "looks": rows(con, "SELECT * FROM looks WHERE production_id=?", (pid,)),
            "items": rows(con, "SELECT * FROM items WHERE production_id=?", (pid,)),
            "look_items": rows(con, "SELECT * FROM look_items"),
            "dressers": rows(con, "SELECT * FROM dressers WHERE production_id=?", (pid,)),
            "positions": rows(con, "SELECT * FROM positions WHERE production_id=?", (pid,)),
            "carts": rows(con, "SELECT * FROM carts WHERE production_id=?", (pid,)),
            "tasks": rows(con, "SELECT * FROM tasks WHERE production_id=? ORDER BY id", (pid,)),
            "revisions": rows(con, "SELECT id,production_id,created_at,note FROM revisions WHERE production_id=? ORDER BY id DESC", (pid,)),
        }
        look_ids = {l["id"] for l in state["looks"]}
        state["look_items"] = [li for li in state["look_items"] if li["look_id"] in look_ids]
        return state
    finally:
        con.close()


def snapshot(production_id=1):
    con = connect()
    try:
        return {
            "scenes": rows(con, "SELECT * FROM scenes WHERE production_id=?", (production_id,)),
            "tasks": rows(con, "SELECT * FROM tasks WHERE production_id=?", (production_id,)),
            "items": rows(con, "SELECT * FROM items WHERE production_id=?", (production_id,)),
        }
    finally:
        con.close()


def save_revision(note, production_id=1):
    con = connect()
    try:
        snap = snapshot(production_id)
        con.execute(
            "INSERT INTO revisions(production_id,created_at,note,snapshot) VALUES(?,?,?,?)",
            (production_id, time.time(), note, json.dumps(snap, ensure_ascii=False)),
        )
        con.commit()
    finally:
        con.close()


def restore_revision(rev_id):
    con = connect()
    try:
        rev = row(con, "SELECT * FROM revisions WHERE id=?", (rev_id,))
        if not rev:
            return False
        snap = json.loads(rev["snapshot"])
        pid = rev["production_id"]
        for table in ("scenes", "tasks", "items"):
            con.execute(f"DELETE FROM {table} WHERE production_id=?", (pid,))
            for r in snap.get(table, []):
                cols = [c for c in r.keys() if c != "id"]
                con.execute(
                    f"INSERT INTO {table}(id,{','.join(cols)}) VALUES(?{',?'*len(cols)})",
                    [r["id"]] + [r[c] for c in cols],
                )
        con.commit()
        return True
    finally:
        con.close()
