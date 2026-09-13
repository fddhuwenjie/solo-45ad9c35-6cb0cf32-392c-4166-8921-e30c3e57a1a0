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
  copies INTEGER NOT NULL DEFAULT 1,
  skill_id INTEGER               -- 该服装穿/脱动作的默认所需技能
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
CREATE TABLE IF NOT EXISTS skills(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dresser_skills(
  dresser_id INTEGER NOT NULL,
  skill_id INTEGER NOT NULL,
  PRIMARY KEY(dresser_id, skill_id)
);
CREATE TABLE IF NOT EXISTS dresser_sides(
  dresser_id INTEGER NOT NULL,
  side TEXT NOT NULL,              -- L | R；无记录=两侧均可（旧数据兼容）
  PRIMARY KEY(dresser_id, side)
);
CREATE TABLE IF NOT EXISTS dresser_unavailable(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL DEFAULT 1,
  dresser_id INTEGER NOT NULL,
  start_sec INTEGER NOT NULL,
  end_sec INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT ''
);
-- 动作规格：某任务内具体穿/脱动作所需技能与人数；按 (kind,item_id,seq) 定位动作
CREATE TABLE IF NOT EXISTS action_specs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  task_id INTEGER NOT NULL,
  kind TEXT NOT NULL,             -- don | doff | fetch | walk
  item_id INTEGER,                -- walk 动作为 NULL
  seq INTEGER NOT NULL DEFAULT 0, -- 同类动作序号（两条 walk：0 退场、1 上场）
  skill_id INTEGER,
  required_count INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS action_specs_uidx
  ON action_specs(task_id, kind, COALESCE(item_id, -1), seq);
-- 动作分工：每行一个参与服装师；is_lead=固定负责人；locked=连排确认后冻结
CREATE TABLE IF NOT EXISTS action_staff(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  task_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  item_id INTEGER,
  seq INTEGER NOT NULL DEFAULT 0,
  dresser_id INTEGER NOT NULL,
  is_lead INTEGER NOT NULL DEFAULT 0,
  locked INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
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
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  revision_id INTEGER NOT NULL,  -- 开启连排所依据的基准修订
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',   -- open | done
  plan TEXT NOT NULL DEFAULT '{}',       -- 开启时冻结的基准计划快照（动作/窗口/资源）
  created_at REAL NOT NULL,
  closed_at REAL
);
CREATE TABLE IF NOT EXISTS run_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  task_id INTEGER NOT NULL,
  action_idx INTEGER NOT NULL,   -- 动作在任务内的序号（对应 plan.actions）
  kind TEXT NOT NULL,            -- start | done | skip | exception
  at_sec INTEGER NOT NULL,       -- 实测时刻（演出时钟秒）
  reason TEXT NOT NULL DEFAULT '', -- 异常/补正理由
  supersedes INTEGER,            -- 补正：被本事件替代的早前事件 id
  item_id INTEGER,               -- 现场实际使用的服装/道具（NULL=按计划）
  copy_id INTEGER,               -- 现场实际使用的副本编号（NULL=未指定）
  created_at REAL NOT NULL
);
"""


def _migrate(con):
    """对已有库做增量列迁移（CREATE TABLE IF NOT EXISTS 不会补列）。"""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(run_events)")}
    for col in ("item_id", "copy_id"):
        if cols and col not in cols:
            con.execute(f"ALTER TABLE run_events ADD COLUMN {col} INTEGER")
    # 实测参与者：实际参与该动作的服装师 id 列表（JSON）；NULL/空=按冻结分工
    for col in ("dresser_ids",):
        if cols and col not in cols:
            con.execute(f"ALTER TABLE run_events ADD COLUMN {col} TEXT")
    icols = {r["name"] for r in con.execute("PRAGMA table_info(items)")}
    if icols and "skill_id" not in icols:
        con.execute("ALTER TABLE items ADD COLUMN skill_id INTEGER")
    con.commit()


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    con = connect()
    con.executescript(SCHEMA)
    _migrate(con)
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
            "skills": rows(con, "SELECT * FROM skills WHERE production_id=? ORDER BY id", (pid,)),
            "dresser_skills": rows(con, "SELECT * FROM dresser_skills"),
            "dresser_sides": rows(con, "SELECT * FROM dresser_sides"),
            "dresser_unavailable": rows(con,
                "SELECT * FROM dresser_unavailable WHERE production_id=? ORDER BY start_sec", (pid,)),
            "action_specs": rows(con,
                "SELECT * FROM action_specs WHERE production_id=? ORDER BY task_id,id", (pid,)),
            "action_staff": rows(con,
                "SELECT * FROM action_staff WHERE production_id=? ORDER BY task_id,id", (pid,)),
            "positions": rows(con, "SELECT * FROM positions WHERE production_id=?", (pid,)),
            "carts": rows(con, "SELECT * FROM carts WHERE production_id=?", (pid,)),
            "tasks": rows(con, "SELECT * FROM tasks WHERE production_id=? ORDER BY id", (pid,)),
            "revisions": rows(con, "SELECT id,production_id,created_at,note FROM revisions WHERE production_id=? ORDER BY id DESC", (pid,)),
        }
        look_ids = {l["id"] for l in state["looks"]}
        state["look_items"] = [li for li in state["look_items"] if li["look_id"] in look_ids]
        dr_ids = {d["id"] for d in state["dressers"]}
        state["dresser_skills"] = [r for r in state["dresser_skills"]
                                   if r["dresser_id"] in dr_ids]
        state["dresser_sides"] = [r for r in state["dresser_sides"]
                                  if r["dresser_id"] in dr_ids]
        return state
    finally:
        con.close()


def snapshot(production_id=1):
    """排程所需的完整状态：场次/任务/服装 + 造型、造型-服装关系、人员、
    换装位、服装车等辅助表。派生修订时据此重建冻结基准，不读当前可变表。
    （restore 仍只恢复 scenes/tasks/items，保持既有行为。）"""
    con = connect()
    try:
        pid = production_id
        looks = rows(con, "SELECT * FROM looks WHERE production_id=?", (pid,))
        look_ids = {l["id"] for l in looks}
        dr_ids = {r["id"] for r in con.execute(
            "SELECT id FROM dressers WHERE production_id=?", (pid,)).fetchall()}
        return {
            "scenes": rows(con, "SELECT * FROM scenes WHERE production_id=?", (pid,)),
            "tasks": rows(con, "SELECT * FROM tasks WHERE production_id=?", (pid,)),
            "items": rows(con, "SELECT * FROM items WHERE production_id=?", (pid,)),
            "looks": looks,
            "look_items": [li for li in rows(con, "SELECT * FROM look_items")
                           if li["look_id"] in look_ids],
            "actors": rows(con, "SELECT * FROM actors WHERE production_id=?", (pid,)),
            "dressers": rows(con, "SELECT * FROM dressers WHERE production_id=?", (pid,)),
            "skills": rows(con, "SELECT * FROM skills WHERE production_id=? ORDER BY id", (pid,)),
            "dresser_skills": [r for r in rows(con, "SELECT * FROM dresser_skills")
                               if r["dresser_id"] in dr_ids],
            "dresser_sides": [r for r in rows(con, "SELECT * FROM dresser_sides")
                              if r["dresser_id"] in dr_ids],
            "dresser_unavailable": rows(con,
                "SELECT * FROM dresser_unavailable WHERE production_id=? ORDER BY start_sec", (pid,)),
            "action_specs": rows(con,
                "SELECT * FROM action_specs WHERE production_id=? ORDER BY task_id,id", (pid,)),
            "action_staff": [r for r in rows(con,
                "SELECT * FROM action_staff WHERE production_id=? ORDER BY task_id,id", (pid,))],
            "positions": rows(con, "SELECT * FROM positions WHERE production_id=?", (pid,)),
            "carts": rows(con, "SELECT * FROM carts WHERE production_id=?", (pid,)),
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


def save_snapshot_revision(snap, note, production_id=1):
    """直接把给定快照（scenes/tasks/items）存为新修订——用于从基准派生，
    不触碰当前可变方案。"""
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO revisions(production_id,created_at,note,snapshot) VALUES(?,?,?,?)",
            (production_id, time.time(), note, json.dumps(snap, ensure_ascii=False)))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def get_revision(rev_id):
    con = connect()
    try:
        return row(con, "SELECT * FROM revisions WHERE id=?", (rev_id,))
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
        # 动作级协作表：修订快照带冻结分工时一并恢复（旧修订无这些键则跳过）
        for table in ("skills", "action_specs", "action_staff", "dresser_unavailable"):
            if table not in snap:
                continue
            con.execute(f"DELETE FROM {table} WHERE production_id=?", (pid,))
            for r in snap[table]:
                cols = [c for c in r.keys() if c != "id"]
                con.execute(
                    f"INSERT INTO {table}(id,{','.join(cols)}) VALUES(?,{','.join('?' for _ in cols)})",
                    [r.get("id")] + [r[c] for c in cols],
                )
        # 全局关联表（无 production_id）：按快照内服装师集合整体替换
        for table in ("dresser_skills", "dresser_sides"):
            if table not in snap:
                continue
            snap_dr_ids = {r["dresser_id"] for r in snap[table]}
            if snap_dr_ids:
                con.executemany(
                    f"DELETE FROM {table} WHERE dresser_id=?",
                    [(i,) for i in snap_dr_ids])
            for r in snap[table]:
                cols = list(r.keys())
                con.execute(
                    f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                    [r[c] for c in cols])
        con.commit()
        return True
    finally:
        con.close()


# ---------------- 连排实测 ----------------
# 实测数据只追加、不改写基准方案：runs.plan 是开启连排时冻结的计划快照，
# run_events 仅允许插入（补正通过 supersedes 链接，旧事件保留作证）。

def create_run(revision_id, name, plan, production_id=1):
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO runs(production_id,revision_id,name,status,plan,created_at) "
            "VALUES(?,?,?,'open',?,?)",
            (production_id, revision_id, name, json.dumps(plan, ensure_ascii=False),
             time.time()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def list_runs(production_id=1):
    con = connect()
    try:
        return rows(con, "SELECT id,production_id,revision_id,name,status,created_at,closed_at "
                         "FROM runs WHERE production_id=? ORDER BY id DESC", (production_id,))
    finally:
        con.close()


def get_run(run_id):
    con = connect()
    try:
        return row(con, "SELECT * FROM runs WHERE id=?", (run_id,))
    finally:
        con.close()


def close_run(run_id):
    con = connect()
    try:
        con.execute("UPDATE runs SET status='done', closed_at=? WHERE id=? AND status='open'",
                    (time.time(), run_id))
        con.commit()
        return True
    finally:
        con.close()


def add_event(run_id, task_id, action_idx, kind, at_sec, reason="", supersedes=None,
              item_id=None, copy_id=None, dresser_ids=None):
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO run_events(run_id,task_id,action_idx,kind,at_sec,reason,supersedes,"
            "item_id,copy_id,dresser_ids,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, task_id, action_idx, kind, int(at_sec), reason, supersedes,
             item_id, copy_id,
             json.dumps(sorted(set(dresser_ids)), ensure_ascii=False) if dresser_ids else None,
             time.time()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def run_events(run_id):
    con = connect()
    try:
        return rows(con, "SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run_id,))
    finally:
        con.close()
