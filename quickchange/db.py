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
  skill_id INTEGER,              -- 该服装穿/脱动作的默认所需技能
  closure TEXT NOT NULL DEFAULT 'zip', -- 默认闭合件：zip拉链|hook钩扣|tie系带|frog盘扣
  -- 场间复位：该类服装副本日场穿用后默认需要的养护工序（道具默认全 0）
  need_clean INTEGER NOT NULL DEFAULT 1,  -- 去渍/清洁
  need_dry INTEGER NOT NULL DEFAULT 1,    -- 烘干
  need_press INTEGER NOT NULL DEFAULT 1   -- 整烫
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
-- 动作级待复核：人员资料/场次/服装变化只标记受影响动作（不整任务标红）
CREATE TABLE IF NOT EXISTS action_reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL DEFAULT 1,
  task_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  item_id INTEGER,
  seq INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
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

-- ---------------- 替演推演 ----------------
-- 演员关键尺寸（厘米）：候补能否穿下原角副本、穿脱加时都据此计算
CREATE TABLE IF NOT EXISTS actor_measures(
  production_id INTEGER NOT NULL,
  actor_id INTEGER NOT NULL,
  height REAL, chest REAL, waist REAL, hip REAL, shoulder REAL, foot REAL,
  updated_at REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(production_id, actor_id)
);
-- 角色候补顺位：role_actor_id=原角演员，under_actor_id=候补，priority 小者优先
CREATE TABLE IF NOT EXISTS understudy_roster(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  role_actor_id INTEGER NOT NULL,
  under_actor_id INTEGER NOT NULL,
  priority INTEGER NOT NULL DEFAULT 1,
  UNIQUE(production_id, role_actor_id, under_actor_id)
);
-- 服装实物副本：闭合件类型可与服装默认不同（拉链/钩扣/系带/盘扣）
CREATE TABLE IF NOT EXISTS item_copies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL,
  copy_no INTEGER NOT NULL,        -- 1 起，与 items.copies 对应
  label TEXT NOT NULL DEFAULT '',
  closure TEXT NOT NULL DEFAULT '',-- '' = 用 items.closure 默认值
  UNIQUE(production_id, item_id, copy_no)
);
-- 副本适配区间：每个可调部位一行；alterable=可改衣，alter_sec=改衣耗时
CREATE TABLE IF NOT EXISTS copy_fit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL DEFAULT 1,
  copy_id INTEGER NOT NULL,
  dim TEXT NOT NULL,               -- height|chest|waist|hip|shoulder|foot
  lo REAL NOT NULL,
  hi REAL NOT NULL,
  alterable INTEGER NOT NULL DEFAULT 0,
  alter_sec INTEGER NOT NULL DEFAULT 0,
  UNIQUE(copy_id, dim)
);
-- 替演分支：从某修订拖换卡司试算；draft 可改，confirmed 冻结
CREATE TABLE IF NOT EXISTS understudy_branches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  revision_id INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',   -- draft | confirmed
  cast_json TEXT NOT NULL DEFAULT '{}',   -- {task_id: under_actor_id}
  assigns_json TEXT NOT NULL DEFAULT '{}',-- {"task_id:item_id": copy_id} 人工改派
  notes_json TEXT NOT NULL DEFAULT '{}',  -- {"task_id:item_id": 备注}
  alter_start_sec INTEGER NOT NULL DEFAULT 0, -- 改衣可开始的演出时钟时刻
  plan_json TEXT,                         -- 最近一次推演（确认后冻结）
  needs_review INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  confirmed_at REAL
);

-- ---------------- 场间复位工作区 ----------------
-- 养护设备（清洁机/烘干机）与工位（缝补台/整烫台/装车口）：
-- 均为「区间日历」资源：capacity 并发容量，cool_down_sec 占用结束后
-- （冷却间隔，含烘干部件冷却）设备/工位不可再排的间隔秒数。
CREATE TABLE IF NOT EXISTS care_resources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,        -- clean | dry | mend | press | load
  is_station INTEGER NOT NULL DEFAULT 0,  -- 0=设备（清洁/烘干），1=工位
  capacity INTEGER NOT NULL DEFAULT 1,
  cool_down_sec INTEGER NOT NULL DEFAULT 0,
  skill_id INTEGER
);
-- 复位工作区：从选定连排的实际穿用记录或已确认方案（修订）生成；
-- evening_offset_sec 把当晚场次时间轴平移到日场时钟（日场结束=养护窗口起点）。
CREATE TABLE IF NOT EXISTS turnarounds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',    -- open | archived
  source_kind TEXT NOT NULL,              -- run | revision
  source_id INTEGER NOT NULL,             -- runs.id 或 revisions.id
  evening_offset_sec INTEGER NOT NULL DEFAULT 0,
  source_json TEXT NOT NULL DEFAULT '{}', -- 生成时来源摘要（归档随附）
  archived_json TEXT,                     -- 归档包：来源记录+人工计划+执行事件
  created_at REAL NOT NULL,
  archived_at REAL
);
-- 逐副本养护路线：一件实际穿用副本一行；deadline_sec=晚场造型就位期限
CREATE TABLE IF NOT EXISTS care_routes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  turnaround_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL,
  copy_no INTEGER NOT NULL,              -- 1 起，对应 item_copies.copy_no
  item_name TEXT NOT NULL DEFAULT '',
  released_at INTEGER NOT NULL DEFAULT 0,  -- 日场脱下可收回养护的时刻
  deadline_sec INTEGER,                   -- 晚场就位期限（NULL=晚场不再引用）
  ref_task_ids TEXT NOT NULL DEFAULT '[]', -- 晚场引用该副本的换装任务 id
  sort_key INTEGER NOT NULL DEFAULT 0,
  UNIQUE(turnaround_id, item_id, copy_no)
);
-- 路线上的有序工序：clean→dry→mend→press→load（按 kind 固定次序）
CREATE TABLE IF NOT EXISTS care_steps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  turnaround_id INTEGER NOT NULL,
  route_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,                  -- 路线内序号，0 起
  kind TEXT NOT NULL,                    -- clean | dry | mend | press | load
  dur_sec INTEGER NOT NULL,
  resource_id INTEGER,                   -- 指派设备/工位（NULL=待排）
  dresser_id INTEGER,                    -- 指派人员（NULL=待排）
  start_sec INTEGER,                     -- 人工钉死的开始时刻（NULL=自动）
  locked INTEGER NOT NULL DEFAULT 0,     -- 完工锁定后不再挪动
  note TEXT NOT NULL DEFAULT ''
);
-- 执行事件：start 开工 / done 完工 / return 退回重做 / scrap 报废；
-- 只追加，退回与报废由计算层解释，不改写已发生事件。
CREATE TABLE IF NOT EXISTS care_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  turnaround_id INTEGER NOT NULL,
  step_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                    -- start | done | return | scrap
  at_sec INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

-- ---------------- 侧台通行推演（路网） ----------------
-- 通道节点：corridor=普通节点；door=门洞（容量1，通过需 dwell_sec，窄门排队）
CREATE TABLE IF NOT EXISTS net_nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  x REAL NOT NULL,
  y REAL NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'corridor',   -- corridor | door
  dwell_sec INTEGER NOT NULL DEFAULT 0
);
-- 路段：净宽/通行耗时/容量/单向；traverse_sec=0 时按长度÷速度估算；
-- capacity=0 时按净宽推导（每 0.6m 容 1 人）
CREATE TABLE IF NOT EXISTS net_edges(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  a_node INTEGER NOT NULL,
  b_node INTEGER NOT NULL,
  width_m REAL NOT NULL DEFAULT 1.5,
  traverse_sec INTEGER NOT NULL DEFAULT 0,
  capacity INTEGER NOT NULL DEFAULT 0,
  oneway INTEGER NOT NULL DEFAULT 0        -- 0=双向，1=仅 a→b
);
-- 路段封闭时段：随场次（scene_id 的演出时段）或显式起止
CREATE TABLE IF NOT EXISTS net_closures(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  edge_id INTEGER NOT NULL,
  scene_id INTEGER,
  start_sec INTEGER,
  end_sec INTEGER,
  reason TEXT NOT NULL DEFAULT ''
);
-- 禁行区：多边形，落区内的节点与路段不可通行
CREATE TABLE IF NOT EXISTS net_zones(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  points_json TEXT NOT NULL DEFAULT '[]'
);
-- 上场口吸附：侧 → 路网节点
CREATE TABLE IF NOT EXISTS net_exits(
  production_id INTEGER NOT NULL,
  side TEXT NOT NULL,                      -- L | R
  node_id INTEGER NOT NULL,
  PRIMARY KEY(production_id, side)
);
-- 人工计划：拖改路径（某任务某移动体的指定节点序列）
CREATE TABLE IF NOT EXISTS net_paths(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  task_id INTEGER NOT NULL,
  mover_kind TEXT NOT NULL,                -- actor | dresser | cart
  mover_id INTEGER NOT NULL,
  nodes_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL
);
-- 让行顺序：指定移动体在该路段优先通行
CREATE TABLE IF NOT EXISTS net_yields(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL,
  edge_id INTEGER NOT NULL,
  mover_kind TEXT NOT NULL,
  mover_id INTEGER NOT NULL
);
-- 路网版本：任何路网变更递增，随修订快照留存
CREATE TABLE IF NOT EXISTS net_meta(
  production_id INTEGER PRIMARY KEY,
  version INTEGER NOT NULL DEFAULT 1
);
-- 最近一次通行推演（逐段时刻）：增量重算定位受影响任务，修订快照留存
CREATE TABLE IF NOT EXISTS net_sim(
  production_id INTEGER PRIMARY KEY,
  version INTEGER NOT NULL DEFAULT 0,
  legs_json TEXT NOT NULL DEFAULT '[]',
  updated_at REAL NOT NULL DEFAULT 0
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
    if icols and "closure" not in icols:
        con.execute("ALTER TABLE items ADD COLUMN closure TEXT NOT NULL DEFAULT 'zip'")
    # 场间复位：旧库补齐工序需求列；道具默认无需养护
    for col, dflt in (("need_clean", 1), ("need_dry", 1), ("need_press", 1)):
        if icols and col not in icols:
            con.execute(f"ALTER TABLE items ADD COLUMN {col} INTEGER NOT NULL DEFAULT {dflt}")
            con.execute(
                f"UPDATE items SET {col}=0 WHERE kind='prop'")
    # 侧台通行：换装位/服装车吸附到路网节点（NULL=按坐标就近吸附）
    pcols = {r["name"] for r in con.execute("PRAGMA table_info(positions)")}
    if pcols and "node_id" not in pcols:
        con.execute("ALTER TABLE positions ADD COLUMN node_id INTEGER")
    ccols = {r["name"] for r in con.execute("PRAGMA table_info(carts)")}
    if ccols and "node_id" not in ccols:
        con.execute("ALTER TABLE carts ADD COLUMN node_id INTEGER")
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
            "action_reviews": rows(con,
                "SELECT * FROM action_reviews WHERE production_id=? ORDER BY id", (pid,)),
            "positions": rows(con, "SELECT * FROM positions WHERE production_id=?", (pid,)),
            "carts": rows(con, "SELECT * FROM carts WHERE production_id=?", (pid,)),
            "tasks": rows(con, "SELECT * FROM tasks WHERE production_id=? ORDER BY id", (pid,)),
            "actor_measures": rows(con, "SELECT * FROM actor_measures WHERE production_id=?", (pid,)),
            "understudy_roster": rows(con,
                "SELECT * FROM understudy_roster WHERE production_id=? ORDER BY role_actor_id,priority,id",
                (pid,)),
            "item_copies": rows(con, "SELECT * FROM item_copies WHERE production_id=? ORDER BY item_id,copy_no", (pid,)),
            "copy_fit": rows(con,
                "SELECT cf.* FROM copy_fit cf JOIN item_copies ic ON ic.id=cf.copy_id "
                "WHERE ic.production_id=? ORDER BY cf.copy_id,cf.dim", (pid,)),
            "revisions": rows(con, "SELECT id,production_id,created_at,note FROM revisions WHERE production_id=? ORDER BY id DESC", (pid,)),
            "care_resources": rows(con,
                "SELECT * FROM care_resources WHERE production_id=? ORDER BY is_station,kind,id",
                (pid,)),
            "turnarounds": rows(con,
                "SELECT id,production_id,name,status,source_kind,source_id,evening_offset_sec,"
                "created_at,archived_at FROM turnarounds WHERE production_id=? ORDER BY id DESC",
                (pid,)),
            "net_nodes": rows(con,
                "SELECT * FROM net_nodes WHERE production_id=? ORDER BY id", (pid,)),
            "net_edges": rows(con,
                "SELECT * FROM net_edges WHERE production_id=? ORDER BY id", (pid,)),
            "net_closures": rows(con,
                "SELECT * FROM net_closures WHERE production_id=? ORDER BY id", (pid,)),
            "net_zones": rows(con,
                "SELECT * FROM net_zones WHERE production_id=? ORDER BY id", (pid,)),
            "net_exits": rows(con,
                "SELECT * FROM net_exits WHERE production_id=?", (pid,)),
            "net_paths": rows(con,
                "SELECT * FROM net_paths WHERE production_id=? ORDER BY id", (pid,)),
            "net_yields": rows(con,
                "SELECT * FROM net_yields WHERE production_id=? ORDER BY id", (pid,)),
        }
        nm = row(con, "SELECT version FROM net_meta WHERE production_id=?", (pid,))
        state["net_version"] = nm["version"] if nm else 0
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
            "action_reviews": rows(con,
                "SELECT * FROM action_reviews WHERE production_id=? ORDER BY id", (pid,)),
            "positions": rows(con, "SELECT * FROM positions WHERE production_id=?", (pid,)),
            "carts": rows(con, "SELECT * FROM carts WHERE production_id=?", (pid,)),
            "actor_measures": rows(con, "SELECT * FROM actor_measures WHERE production_id=?", (pid,)),
            "understudy_roster": rows(con,
                "SELECT * FROM understudy_roster WHERE production_id=? ORDER BY role_actor_id,priority,id",
                (pid,)),
            "item_copies": rows(con, "SELECT * FROM item_copies WHERE production_id=? ORDER BY item_id,copy_no", (pid,)),
            "copy_fit": rows(con,
                "SELECT cf.* FROM copy_fit cf JOIN item_copies ic ON ic.id=cf.copy_id "
                "WHERE ic.production_id=? ORDER BY cf.copy_id,cf.dim", (pid,)),
            # 侧台通行路网：版本、路网、人工计划（拖改路径/让行）与逐段时刻随修订留存
            "net_nodes": rows(con, "SELECT * FROM net_nodes WHERE production_id=?", (pid,)),
            "net_edges": rows(con, "SELECT * FROM net_edges WHERE production_id=?", (pid,)),
            "net_closures": rows(con, "SELECT * FROM net_closures WHERE production_id=?", (pid,)),
            "net_zones": rows(con, "SELECT * FROM net_zones WHERE production_id=?", (pid,)),
            "net_exits": rows(con, "SELECT * FROM net_exits WHERE production_id=?", (pid,)),
            "net_paths": rows(con, "SELECT * FROM net_paths WHERE production_id=?", (pid,)),
            "net_yields": rows(con, "SELECT * FROM net_yields WHERE production_id=?", (pid,)),
            "net_version": (row(con, "SELECT version FROM net_meta WHERE production_id=?",
                                (pid,)) or {"version": 0})["version"],
            "net_sim": row(con, "SELECT version,legs_json,updated_at FROM net_sim "
                                "WHERE production_id=?", (pid,)),
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
        # 侧台通行路网（含人工计划与版本）：旧修订无这些键则跳过
        for table in ("net_nodes", "net_edges", "net_closures", "net_zones",
                      "net_exits", "net_paths", "net_yields"):
            if table not in snap:
                continue
            con.execute(f"DELETE FROM {table} WHERE production_id=?", (pid,))
            for r in snap[table]:
                cols = [c for c in r.keys() if c != "id"]
                con.execute(
                    f"INSERT INTO {table}(id,{','.join(cols)}) VALUES(?{',?'*len(cols)})",
                    [r.get("id")] + [r[c] for c in cols],
                )
        if "net_version" in snap:
            con.execute(
                "INSERT INTO net_meta(production_id,version) VALUES(?,?) "
                "ON CONFLICT(production_id) DO UPDATE SET version=excluded.version",
                (pid, int(snap["net_version"] or 0)))
        if snap.get("net_sim"):
            ns = snap["net_sim"]
            con.execute(
                "INSERT INTO net_sim(production_id,version,legs_json,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(production_id) DO UPDATE SET "
                "version=excluded.version,legs_json=excluded.legs_json,"
                "updated_at=excluded.updated_at",
                (pid, int(ns.get("version") or 0), ns.get("legs_json") or "[]",
                 float(ns.get("updated_at") or 0)))
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
             json.dumps(sorted(set(dresser_ids)), ensure_ascii=False)
             if dresser_ids is not None else None,
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


# ---------------- 替演推演 ----------------

def sync_item_copies(pid=1):
    """按 items.copies 物化实物副本行（新增补齐；件数减少不删行，保留适配资料）。"""
    con = connect()
    try:
        items = rows(con, "SELECT id, copies FROM items WHERE production_id=?", (pid,))
        for it in items:
            have = {r["copy_no"] for r in con.execute(
                "SELECT copy_no FROM item_copies WHERE item_id=?", (it["id"],))}
            for n in range(1, max(1, int(it["copies"])) + 1):
                if n not in have:
                    con.execute(
                        "INSERT INTO item_copies(production_id,item_id,copy_no,label,closure) "
                        "VALUES(?,?,?,'','')", (pid, it["id"], n))
        con.commit()
    finally:
        con.close()


def list_branches(pid=1):
    con = connect()
    try:
        return rows(con,
            "SELECT id,production_id,revision_id,name,status,needs_review,created_at,confirmed_at "
            "FROM understudy_branches WHERE production_id=? ORDER BY id DESC", (pid,))
    finally:
        con.close()


def get_branch(branch_id):
    con = connect()
    try:
        return row(con, "SELECT * FROM understudy_branches WHERE id=?", (branch_id,))
    finally:
        con.close()


def create_branch(revision_id, name, cast, assigns, notes, alter_start_sec, plan, pid=1):
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO understudy_branches(production_id,revision_id,name,status,cast_json,"
            "assigns_json,notes_json,alter_start_sec,plan_json,needs_review,created_at) "
            "VALUES(?,?,?,'draft',?,?,?,?,?,0,?)",
            (pid, revision_id, name,
             json.dumps(cast, ensure_ascii=False), json.dumps(assigns, ensure_ascii=False),
             json.dumps(notes, ensure_ascii=False), int(alter_start_sec or 0),
             json.dumps(plan, ensure_ascii=False) if plan is not None else None,
             time.time()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def update_branch(branch_id, cast=None, assigns=None, notes=None,
                  alter_start_sec=None, plan=None, needs_review=None):
    """整体更新草稿分支的试算输入与最近一次推演结果。"""
    sets, args = [], []
    if cast is not None:
        sets.append("cast_json=?")
        args.append(json.dumps(cast, ensure_ascii=False))
    if assigns is not None:
        sets.append("assigns_json=?")
        args.append(json.dumps(assigns, ensure_ascii=False))
    if notes is not None:
        sets.append("notes_json=?")
        args.append(json.dumps(notes, ensure_ascii=False))
    if alter_start_sec is not None:
        sets.append("alter_start_sec=?")
        args.append(int(alter_start_sec))
    if plan is not None:
        sets.append("plan_json=?")
        args.append(json.dumps(plan, ensure_ascii=False))
    if needs_review is not None:
        sets.append("needs_review=?")
        args.append(1 if needs_review else 0)
    if not sets:
        return
    con = connect()
    try:
        con.execute(f"UPDATE understudy_branches SET {','.join(sets)} WHERE id=?",
                    args + [branch_id])
        con.commit()
    finally:
        con.close()


def confirm_branch(branch_id, plan):
    con = connect()
    try:
        con.execute(
            "UPDATE understudy_branches SET status='confirmed', needs_review=0, "
            "plan_json=?, confirmed_at=? WHERE id=?",
            (json.dumps(plan, ensure_ascii=False), time.time(), branch_id))
        con.commit()
    finally:
        con.close()


def delete_branch(branch_id):
    con = connect()
    try:
        con.execute("DELETE FROM understudy_branches WHERE id=?", (branch_id,))
        con.commit()
    finally:
        con.close()


def mark_understudy_dirty(pid, actor_ids=None, item_ids=None):
    """资料变化时只标记相关替演分支待复核（已确认分支计划不重算，只打标记）：
    - 演员（含原角与候补）尺寸变化：卡司引用了该演员（任一拖换任务的原角
      或候补）的分支——候补尺寸决定适配，原角尺寸决定尺寸偏差加时；
    - 服装/副本/适配变化：基准快照中含该服装的分支。"""
    con = connect()
    try:
        actor_ids = {int(x) for x in (actor_ids or [])}
        item_ids = {int(x) for x in (item_ids or [])}
        branches = rows(con,
            "SELECT b.id, b.cast_json, b.revision_id FROM understudy_branches b "
            "WHERE b.production_id=?", (pid,))
        # 修订快照的 task_id -> 原角 actor_id（用于匹配原角尺寸变化）
        rev_orig = {}
        for b in branches:
            cast = json.loads(b["cast_json"] or "{}")
            orig = set()
            rev = row(con, "SELECT snapshot FROM revisions WHERE id=?", (b["revision_id"],))
            if rev:
                tmap = {t["id"]: t["actor_id"]
                        for t in json.loads(rev["snapshot"]).get("tasks", [])}
                orig = {tmap.get(int(k)) for k in cast} - {None}
            under = {int(v) for v in cast.values()}
            hit = False
            if actor_ids and actor_ids & (under | orig):
                hit = True
            if item_ids and rev:
                snap_items = {i["id"] for i in json.loads(rev["snapshot"]).get("items", [])}
                if item_ids & snap_items:
                    hit = True
            if hit:
                con.execute("UPDATE understudy_branches SET needs_review=1 WHERE id=?", (b["id"],))
        con.commit()
    finally:
        con.close()


# ---------------- 场间复位工作区 ----------------

def get_turnaround(tid):
    con = connect()
    try:
        return row(con, "SELECT * FROM turnarounds WHERE id=?", (tid,))
    finally:
        con.close()


def care_routes(tid):
    con = connect()
    try:
        return rows(con,
            "SELECT * FROM care_routes WHERE turnaround_id=? ORDER BY sort_key,id", (tid,))
    finally:
        con.close()


def care_steps(tid):
    con = connect()
    try:
        return rows(con,
            "SELECT * FROM care_steps WHERE turnaround_id=? ORDER BY route_id,seq,id", (tid,))
    finally:
        con.close()


def care_events(tid):
    con = connect()
    try:
        return rows(con,
            "SELECT * FROM care_events WHERE turnaround_id=? ORDER BY id", (tid,))
    finally:
        con.close()


def care_resources(pid=1):
    con = connect()
    try:
        return rows(con,
            "SELECT * FROM care_resources WHERE production_id=? ORDER BY is_station,kind,id",
            (pid,))
    finally:
        con.close()


def create_turnaround(name, source_kind, source_id, evening_offset, routes_spec,
                      source_summary, pid=1):
    """一次性生成工作区、逐副本路线与默认工序。
    routes_spec: [{item_id, copy_no, item_name, released_at, deadline_sec,
                   ref_task_ids:[...], kinds:[...], sort_key}]
    """
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO turnarounds(production_id,name,status,source_kind,source_id,"
            "evening_offset_sec,source_json,created_at) VALUES(?,?,'open',?,?,?,?,?)",
            (pid, name, source_kind, int(source_id), int(evening_offset or 0),
             json.dumps(source_summary, ensure_ascii=False), time.time()))
        tid = cur.lastrowid
        for r in routes_spec:
            rc = con.execute(
                "INSERT INTO care_routes(production_id,turnaround_id,item_id,copy_no,"
                "item_name,released_at,deadline_sec,ref_task_ids,sort_key) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, tid, r["item_id"], r["copy_no"], r["item_name"],
                 int(r["released_at"]),
                 None if r.get("deadline_sec") is None else int(r["deadline_sec"]),
                 json.dumps(r.get("ref_task_ids", []), ensure_ascii=False),
                 int(r.get("sort_key", 0))))
            rid = rc.lastrowid
            for seq, (kind, dur) in enumerate(r["kinds"]):
                con.execute(
                    "INSERT INTO care_steps(production_id,turnaround_id,route_id,seq,kind,"
                    "dur_sec,locked) VALUES(?,?,?,?,?,?,0)",
                    (pid, tid, rid, seq, kind, int(dur)))
        con.commit()
        return tid
    finally:
        con.close()


def get_care_step(step_id):
    con = connect()
    try:
        return row(con, "SELECT * FROM care_steps WHERE id=?", (step_id,))
    finally:
        con.close()


def update_care_step(step_id, sets):
    if not sets:
        return
    con = connect()
    try:
        con.execute(
            f"UPDATE care_steps SET {','.join(k+'=?' for k in sets)} WHERE id=?",
            list(sets.values()) + [step_id])
        con.commit()
    finally:
        con.close()


def add_care_event(tid, step_id, kind, at_sec, reason, pid=1):
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO care_events(production_id,turnaround_id,step_id,kind,at_sec,"
            "reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (pid, tid, step_id, kind, int(at_sec), reason, time.time()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def archive_turnaround(tid, archive_pack):
    con = connect()
    try:
        con.execute(
            "UPDATE turnarounds SET status='archived', archived_json=?, archived_at=? "
            "WHERE id=?",
            (json.dumps(archive_pack, ensure_ascii=False), time.time(), tid))
        con.commit()
    finally:
        con.close()


def upsert_care_resource(pid, name, kind, is_station, capacity, cool_down_sec,
                         skill_id=None, rid=None):
    con = connect()
    try:
        if rid:
            con.execute(
                "UPDATE care_resources SET name=?,kind=?,is_station=?,capacity=?,"
                "cool_down_sec=?,skill_id=? WHERE id=? AND production_id=?",
                (name, kind, int(is_station), int(capacity), int(cool_down_sec),
                 skill_id, rid, pid))
            return rid
        cur = con.execute(
            "INSERT INTO care_resources(production_id,name,kind,is_station,capacity,"
            "cool_down_sec,skill_id) VALUES(?,?,?,?,?,?,?)",
            (pid, name, kind, int(is_station), int(capacity), int(cool_down_sec), skill_id))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def delete_care_resource(pid, rid):
    con = connect()
    try:
        con.execute("UPDATE care_steps SET resource_id=NULL "
                    "WHERE resource_id=? AND production_id=?", (rid, pid))
        con.execute("DELETE FROM care_resources WHERE id=? AND production_id=?", (rid, pid))
        con.commit()
    finally:
        con.close()


# ---------------- 侧台通行推演 ----------------

def bump_net_version(pid=1):
    """路网变更：版本号递增（随修订快照留存）。"""
    con = connect()
    try:
        con.execute(
            "INSERT INTO net_meta(production_id,version) VALUES(?,1) "
            "ON CONFLICT(production_id) DO UPDATE SET version=version+1", (pid,))
        con.commit()
        return row(con, "SELECT version FROM net_meta WHERE production_id=?",
                   (pid,))["version"]
    finally:
        con.close()


def get_net_sim(pid=1):
    con = connect()
    try:
        return row(con, "SELECT * FROM net_sim WHERE production_id=?", (pid,))
    finally:
        con.close()


def save_net_sim(pid, version, legs):
    """留存最近一次通行推演的逐段时刻（内容不变则不写）。"""
    payload = json.dumps(legs, ensure_ascii=False)
    con = connect()
    try:
        cur = row(con, "SELECT version,legs_json FROM net_sim WHERE production_id=?", (pid,))
        if cur and cur["version"] == version and cur["legs_json"] == payload:
            return
        con.execute(
            "INSERT INTO net_sim(production_id,version,legs_json,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(production_id) DO UPDATE SET "
            "version=excluded.version,legs_json=excluded.legs_json,"
            "updated_at=excluded.updated_at",
            (pid, int(version), payload, time.time()))
        con.commit()
    finally:
        con.close()

