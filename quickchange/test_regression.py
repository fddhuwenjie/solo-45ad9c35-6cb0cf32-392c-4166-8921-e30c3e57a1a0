# -*- coding: utf-8 -*-
"""复核缺陷回归测试：

1. 锁定任务的普通更新被拒绝（start_sec / position_id 不得改动）；
2. 重排时未来的锁定任务只保留自己的固定时段，不会预先占用服装师、
   把更早的任务推迟（复现：早任务被推到 102 秒并误报超时）；
3. 单件服装（copies=1）自穿上起持续占用，直到脱下动作结束；
   没有脱下记录时占用到演员所在场次结束，穿着期间不会重复分配。

运行：python3 test_regression.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import db
import scheduler

PASS = []


def check(name, fn):
    fn()
    PASS.append(name)
    print(f"  ok - {name}")


# ---------- 用例 1：锁定任务的普通更新被拒绝 ----------

def test_locked_update_rejected():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.DB_PATH = tmp
    db.init_db()
    import app as web
    client = web.app.test_client()
    con = db.connect()
    con.execute("INSERT INTO scenes(production_id,seq,name,start_sec,duration_sec) VALUES(1,1,'S1',0,100)")
    con.execute("INSERT INTO scenes(production_id,seq,name,start_sec,duration_sec) VALUES(1,2,'S2',200,100)")
    con.execute("INSERT INTO actors(production_id,name) VALUES(1,'甲')")
    con.execute("INSERT INTO positions(production_id,name,side,x,y,capacity) VALUES(1,'P1','L',5,5,1)")
    con.execute("INSERT INTO positions(production_id,name,side,x,y,capacity) VALUES(1,'P2','L',8,5,1)")
    con.execute("INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,exit_side,"
                "position_id,start_sec,locked) VALUES(1,1,1,2,'L',1,120,1)")
    con.commit()
    con.close()

    r = client.post("/api/tasks/1", json={"start_sec": 130})
    assert r.status_code == 409, f"锁定任务改 start_sec 应 409，实得 {r.status_code}"
    r = client.post("/api/tasks/1", json={"position_id": 2})
    assert r.status_code == 409, f"锁定任务改 position_id 应 409，实得 {r.status_code}"
    con = db.connect()
    row = con.execute("SELECT * FROM tasks WHERE id=1").fetchone()
    con.close()
    assert row["start_sec"] == 120 and row["position_id"] == 1, "锁定任务字段被改动"

    r = client.post("/api/tasks/1", json={"note": "连排已确认"})
    assert r.status_code == 200, "锁定任务的备注等普通字段应可更新"

    r = client.post("/api/tasks/1", json={"locked": 0, "start_sec": 130})
    assert r.status_code == 200, "同请求解锁后应允许调整"
    con = db.connect()
    row = con.execute("SELECT * FROM tasks WHERE id=1").fetchone()
    con.close()
    assert row["locked"] == 0 and row["start_sec"] == 130

    # 重新锁定后，重排不得移动锁定任务
    client.post("/api/tasks/1", json={"locked": 1})
    r = client.post("/api/reschedule")
    assert r.status_code == 200
    con = db.connect()
    row = con.execute("SELECT * FROM tasks WHERE id=1").fetchone()
    con.close()
    assert row["start_sec"] == 130, f"重排移动了锁定任务：{row['start_sec']}"
    os.unlink(tmp)


# ---------- 用例 2：未来锁定任务不挤占更早任务 ----------

def _state_future_locked():
    return {
        "scenes": [
            {"id": 1, "production_id": 1, "seq": 1, "name": "S1", "start_sec": 0, "duration_sec": 50},
            {"id": 2, "production_id": 1, "seq": 2, "name": "S2", "start_sec": 100, "duration_sec": 300},
            {"id": 3, "production_id": 1, "seq": 3, "name": "S3", "start_sec": 500, "duration_sec": 100},
        ],
        "actors": [
            {"id": 1, "production_id": 1, "name": "甲", "code": "", "default_side": "L"},
            {"id": 2, "production_id": 1, "name": "乙", "code": "", "default_side": "L"},
        ],
        "items": [
            {"id": 1, "production_id": 1, "name": "短衣", "kind": "costume", "layer": 1,
             "don_sec": 10, "doff_sec": 8, "status": "ok", "available_at": 0, "cart_id": None, "copies": 2},
            {"id": 2, "production_id": 1, "name": "长袍", "kind": "costume", "layer": 2,
             "don_sec": 10, "doff_sec": 8, "status": "ok", "available_at": 0, "cart_id": None, "copies": 2},
        ],
        "looks": [
            {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
            {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""},
            {"id": 3, "production_id": 1, "actor_id": 2, "scene_id": 2, "name": ""},
            {"id": 4, "production_id": 1, "actor_id": 2, "scene_id": 3, "name": ""},
        ],
        "look_items": [
            {"look_id": 1, "item_id": 1, "ord": 0},
            {"look_id": 2, "item_id": 2, "ord": 0},
            {"look_id": 3, "item_id": 1, "ord": 0},
            {"look_id": 4, "item_id": 2, "ord": 0},
        ],
        "dressers": [{"id": 1, "production_id": 1, "name": "王姐"}],
        "positions": [{"id": 1, "production_id": 1, "name": "P", "side": "L",
                       "x": 6, "y": 4, "capacity": 1}],
        "carts": [],
        "tasks": [
            # 早任务：窗口 50→100，约需 30s
            {"id": 1, "production_id": 1, "actor_id": 1, "from_scene_id": 1, "to_scene_id": 2,
             "exit_side": "L", "position_id": 1, "dresser_id": 1, "start_sec": None,
             "locked": 0, "needs_review": 0, "note": ""},
            # 未来锁定任务：固定 420 开始，同一位服装师
            {"id": 2, "production_id": 1, "actor_id": 2, "from_scene_id": 2, "to_scene_id": 3,
             "exit_side": "L", "position_id": 1, "dresser_id": 1, "start_sec": 420,
             "locked": 1, "needs_review": 0, "note": ""},
        ],
    }


def test_future_locked_does_not_block_early():
    sched = scheduler.compute_schedule(_state_future_locked())
    w1 = sched["windows"][1]
    assert w1["start"] == 50, f"早任务被推迟到 {w1['start']}（应在其就绪时刻 50）"
    assert w1["ok"], "早任务被误报超时"
    bad = [c for c in sched["conflicts"] if c["task_id"] == 1 and c["type"] in ("late", "dresser")]
    assert not bad, f"早任务出现误报冲突：{bad}"
    w2 = sched["windows"][2]
    assert w2["start"] == 420, f"锁定任务未保留在固定时段：{w2['start']}"


# ---------- 用例 3：单件服装穿着期间不重复分配 ----------

def _state_single_copy(with_doff_task):
    scenes = [
        {"id": 1, "production_id": 1, "seq": 1, "name": "S1", "start_sec": 0, "duration_sec": 100},
        {"id": 2, "production_id": 1, "seq": 2, "name": "S2", "start_sec": 150, "duration_sec": 100},
        {"id": 3, "production_id": 1, "seq": 3, "name": "S3", "start_sec": 300, "duration_sec": 150},
    ]
    looks = [
        {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
        {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""},
        {"id": 3, "production_id": 1, "actor_id": 1, "scene_id": 3, "name": ""},
        {"id": 5, "production_id": 1, "actor_id": 2, "scene_id": 2, "name": ""},
        {"id": 6, "production_id": 1, "actor_id": 2, "scene_id": 3, "name": ""},
    ]
    look_items = [
        {"look_id": 2, "item_id": 1, "ord": 0},   # 甲 S2 穿斗篷
        {"look_id": 3, "item_id": 1, "ord": 0},   # 甲 S3 仍穿斗篷（无脱下记录）
        {"look_id": 6, "item_id": 1, "ord": 0},   # 乙 S3 也要斗篷
    ]
    tasks = [
        {"id": 1, "production_id": 1, "actor_id": 1, "from_scene_id": 1, "to_scene_id": 2,
         "exit_side": "L", "position_id": None, "dresser_id": None, "start_sec": None,
         "locked": 0, "needs_review": 0, "note": ""},
        {"id": 3, "production_id": 1, "actor_id": 2, "from_scene_id": 2, "to_scene_id": 3,
         "exit_side": "L", "position_id": None, "dresser_id": None, "start_sec": None,
         "locked": 0, "needs_review": 0, "note": ""},
    ]
    if with_doff_task:
        scenes.append({"id": 4, "production_id": 1, "seq": 4, "name": "S4",
                       "start_sec": 500, "duration_sec": 100})
        looks.append({"id": 4, "production_id": 1, "actor_id": 1, "scene_id": 4, "name": ""})
        tasks.append({"id": 2, "production_id": 1, "actor_id": 1, "from_scene_id": 3,
                      "to_scene_id": 4, "exit_side": "L", "position_id": None,
                      "dresser_id": None, "start_sec": None, "locked": 0,
                      "needs_review": 0, "note": ""})
    return {
        "scenes": scenes,
        "actors": [
            {"id": 1, "production_id": 1, "name": "甲", "code": "", "default_side": "L"},
            {"id": 2, "production_id": 1, "name": "乙", "code": "", "default_side": "L"},
        ],
        "items": [{"id": 1, "production_id": 1, "name": "斗篷", "kind": "costume", "layer": 2,
                   "don_sec": 12, "doff_sec": 8, "status": "ok", "available_at": 0,
                   "cart_id": None, "copies": 1}],
        "looks": looks,
        "look_items": look_items,
        "dressers": [],
        "positions": [],
        "carts": [],
        "tasks": tasks,
    }


def test_single_copy_busy_until_doff():
    sched = scheduler.compute_schedule(_state_single_copy(with_doff_task=True))
    don_a = next(a for a in sched["actions"] if a["task_id"] == 1 and a["kind"] == "don")
    doff_a = next(a for a in sched["actions"] if a["task_id"] == 2 and a["kind"] == "doff")
    don_b = next(a for a in sched["actions"] if a["task_id"] == 3 and a["kind"] == "don")
    # 甲在 ~101 穿上后，副本持续占用到脱下动作结束；乙不能在 12 秒穿完后拿到
    assert don_b["start"] >= doff_a["end"], \
        f"副本在穿着期间被重复分配：甲 {don_a['start']}→{doff_a['end']}，乙 {don_b['start']}"
    assert don_b["start"] > don_a["end"], "乙不应在甲刚穿完就拿到唯一副本"
    assert any(c["task_id"] == 3 and c["type"] == "item" for c in sched["conflicts"]), \
        "应报告乙的缺件/复用冲突"


def test_single_copy_busy_until_scene_end_without_doff():
    sched = scheduler.compute_schedule(_state_single_copy(with_doff_task=False))
    don_b = next(a for a in sched["actions"] if a["task_id"] == 3 and a["kind"] == "don")
    scene3_end = 300 + 150
    # 没有脱下记录 → 占用到甲穿着该件的场次（S3）结束
    assert don_b["start"] >= scene3_end, \
        f"无脱下记录时副本应占用到场次结束 {scene3_end}，乙实际 {don_b['start']}"


# ---------- 用例 4：脱下被顺延 → 副本占用延长到实际脱下结束 ----------

def _state_delayed_doff():
    """甲 101 穿上唯一斗篷；甲的脱下任务因服装师被丙占用而顺延，
    实际脱下结束 = 521（估算值仅 399）。乙在 399–411 不得拿到同一副本。"""
    return {
        "scenes": [
            {"id": 1, "production_id": 1, "seq": 1, "name": "S1", "start_sec": 0, "duration_sec": 100},
            {"id": 2, "production_id": 1, "seq": 2, "name": "S2", "start_sec": 150, "duration_sec": 100},
            {"id": 3, "production_id": 1, "seq": 3, "name": "S3", "start_sec": 300, "duration_sec": 90},
            {"id": 4, "production_id": 1, "seq": 4, "name": "S4", "start_sec": 600, "duration_sec": 100},
            {"id": 5, "production_id": 1, "seq": 5, "name": "S5", "start_sec": 335, "duration_sec": 50},
            {"id": 6, "production_id": 1, "seq": 6, "name": "S6", "start_sec": 700, "duration_sec": 100},
        ],
        "actors": [
            {"id": 1, "production_id": 1, "name": "甲", "code": "", "default_side": "L"},
            {"id": 2, "production_id": 1, "name": "乙", "code": "", "default_side": "L"},
            {"id": 3, "production_id": 1, "name": "丙", "code": "", "default_side": "L"},
        ],
        "items": [
            {"id": 1, "production_id": 1, "name": "斗篷", "kind": "costume", "layer": 2,
             "don_sec": 12, "doff_sec": 8, "status": "ok", "available_at": 0,
             "cart_id": None, "copies": 1},
            {"id": 2, "production_id": 1, "name": "帽子", "kind": "costume", "layer": 1,
             "don_sec": 125, "doff_sec": 5, "status": "ok", "available_at": 0,
             "cart_id": None, "copies": 1},
        ],
        "looks": [
            {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
            {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""},
            {"id": 3, "production_id": 1, "actor_id": 1, "scene_id": 3, "name": ""},
            {"id": 4, "production_id": 1, "actor_id": 1, "scene_id": 4, "name": ""},
            {"id": 5, "production_id": 1, "actor_id": 2, "scene_id": 2, "name": ""},
            {"id": 6, "production_id": 1, "actor_id": 2, "scene_id": 3, "name": ""},
            {"id": 7, "production_id": 1, "actor_id": 3, "scene_id": 5, "name": ""},
            {"id": 8, "production_id": 1, "actor_id": 3, "scene_id": 6, "name": ""},
        ],
        "look_items": [
            {"look_id": 2, "item_id": 1, "ord": 0},   # 甲 S2、S3 穿斗篷
            {"look_id": 3, "item_id": 1, "ord": 0},
            {"look_id": 6, "item_id": 1, "ord": 0},   # 乙 S3 要斗篷
            {"look_id": 8, "item_id": 2, "ord": 0},   # 丙 S6 戴帽子（长时间占用服装师）
        ],
        "dressers": [{"id": 1, "production_id": 1, "name": "王姐"}],
        "positions": [],
        "carts": [],
        # 动作级分工：王姐负责甲脱下任务的全部动作（含走位与交接空当）。
        # 动作级模型允许动作交接背靠背（[a,b) 与 [b,c) 不冲突），
        # 登记全部动作后王姐在交接空当仍占用，与旧整段负责人语义一致。
        "action_staff": [
            {"id": 1, "production_id": 1, "task_id": 2, "kind": k,
             "item_id": iid, "seq": sq, "dresser_id": 1, "is_lead": 1,
             "locked": 0, "created_at": 0}
            for k, iid, sq in (
                ("walk", None, 0), ("doff", 2, 0), ("don", 1, 0),
                ("walk", None, 1))
        ],
        "action_specs": [], "skills": [], "dresser_skills": [],
        "dresser_sides": [], "dresser_unavailable": [],
        "tasks": [
            {"id": 1, "production_id": 1, "actor_id": 1, "from_scene_id": 1, "to_scene_id": 2,
             "exit_side": "L", "position_id": None, "dresser_id": None, "start_sec": None,
             "locked": 0, "needs_review": 0, "note": ""},
            {"id": 2, "production_id": 1, "actor_id": 1, "from_scene_id": 3, "to_scene_id": 4,
             "exit_side": "L", "position_id": None, "dresser_id": 1, "start_sec": None,
             "locked": 0, "needs_review": 0, "note": ""},
            {"id": 3, "production_id": 1, "actor_id": 2, "from_scene_id": 2, "to_scene_id": 3,
             "exit_side": "L", "position_id": None, "dresser_id": None, "start_sec": None,
             "locked": 0, "needs_review": 0, "note": ""},
            # 丙的任务 385 就绪，穿帽子 125s，把服装师占用到 512
            {"id": 9, "production_id": 1, "actor_id": 3, "from_scene_id": 5, "to_scene_id": 6,
             "exit_side": "L", "position_id": None, "dresser_id": 1, "start_sec": None,
             "locked": 0, "needs_review": 0, "note": ""},
        ],
    }


def test_delayed_doff_extends_occupancy():
    sched = scheduler.compute_schedule(_state_delayed_doff())
    don_a = next(a for a in sched["actions"] if a["task_id"] == 1 and a["kind"] == "don")
    doff_a = next(a for a in sched["actions"] if a["task_id"] == 2 and a["kind"] == "doff")
    don_b = next(a for a in sched["actions"] if a["task_id"] == 3 and a["kind"] == "don")
    assert don_a["start"] == 101, f"甲穿上时刻应为 101，实得 {don_a['start']}"
    assert doff_a["end"] == 521, \
        f"服装师争用应使脱下顺延到 521 结束，实得 {doff_a['end']}"
    # 甲实际穿着区间 [101, 521)；乙不得在估算释放点 399–411 拿到同一副本
    assert don_b["start"] >= doff_a["end"], \
        f"副本在实际脱下前被重复分配：甲穿到 {doff_a['end']}，乙 {don_b['start']}"
    assert not (399 <= don_b["start"] < 411), \
        f"乙在估算释放窗口 [399,411) 内拿到了副本：{don_b['start']}"
    # 实际占用区间无交集
    assert (don_b["start"], 10**9) and don_b["start"] >= doff_a["end"]
    assert any(c["task_id"] == 3 and c["type"] == "item" for c in sched["conflicts"]), \
        "应报告乙的缺件/复用冲突"


# ---------- 用例 5：七场单副本反例 —— 窗口内后续占用不得被无视 ----------

def _state_seven_scene_single_copy():
    """I2 单副本。甲的任务先排（就绪 20），其 I2 穿上落在 [286,291)；
    乙 26 即可穿上、释放点 322 —— 旧逻辑在 26 发现副本空闲便分配 [26,322)，
    无视窗口内甲的 [286,291)，造成同一副本重复占用。"""
    return {
        "scenes": [
            {"id": 1, "production_id": 1, "seq": 1, "name": "S1", "start_sec": 0, "duration_sec": 20},
            {"id": 2, "production_id": 1, "seq": 2, "name": "S2", "start_sec": 0, "duration_sec": 25},
            {"id": 3, "production_id": 1, "seq": 3, "name": "S3", "start_sec": 30, "duration_sec": 241},
            {"id": 4, "production_id": 1, "seq": 4, "name": "S4", "start_sec": 287, "duration_sec": 4},
            {"id": 5, "production_id": 1, "seq": 5, "name": "S5", "start_sec": 30, "duration_sec": 100},
            {"id": 6, "production_id": 1, "seq": 6, "name": "S6", "start_sec": 150, "duration_sec": 172},
            {"id": 7, "production_id": 1, "seq": 7, "name": "S7", "start_sec": 400, "duration_sec": 100},
        ],
        "actors": [
            {"id": 1, "production_id": 1, "name": "甲", "code": "", "default_side": "L"},
            {"id": 2, "production_id": 1, "name": "乙", "code": "", "default_side": "L"},
            {"id": 3, "production_id": 1, "name": "丙", "code": "", "default_side": "L"},
        ],
        "items": [
            {"id": 1, "production_id": 1, "name": "I1", "kind": "costume", "layer": 1,
             "don_sec": 15, "doff_sec": 5, "status": "ok", "available_at": 0,
             "cart_id": None, "copies": 1},
            {"id": 2, "production_id": 1, "name": "I2", "kind": "costume", "layer": 2,
             "don_sec": 5, "doff_sec": 5, "status": "ok", "available_at": 0,
             "cart_id": None, "copies": 1},
        ],
        "looks": [
            {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
            {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 4, "name": ""},
            {"id": 3, "production_id": 1, "actor_id": 2, "scene_id": 2, "name": ""},
            {"id": 4, "production_id": 1, "actor_id": 2, "scene_id": 5, "name": ""},
            {"id": 5, "production_id": 1, "actor_id": 2, "scene_id": 6, "name": ""},
            {"id": 6, "production_id": 1, "actor_id": 3, "scene_id": 3, "name": ""},
        ],
        "look_items": [
            {"look_id": 2, "item_id": 1, "ord": 0},   # 甲 S4 穿 I1、I2
            {"look_id": 2, "item_id": 2, "ord": 1},
            {"look_id": 4, "item_id": 2, "ord": 0},   # 乙 S5、S6 穿 I2
            {"look_id": 5, "item_id": 2, "ord": 0},
            {"look_id": 6, "item_id": 1, "ord": 0},   # 丙 S3 穿 I1（占用到 271）
        ],
        "dressers": [],
        "positions": [],
        "carts": [],
        "tasks": [
            # 甲：20 就绪；先等 I1 到 271，再穿 I2 → I2 穿上落在 [286,291)
            {"id": 1, "production_id": 1, "actor_id": 1, "from_scene_id": 1, "to_scene_id": 4,
             "exit_side": "L", "position_id": None, "dresser_id": None, "start_sec": None,
             "locked": 0, "needs_review": 0, "note": ""},
            # 乙：25 就绪，26 即可穿 I2，释放点 322（S6 结束）
            {"id": 2, "production_id": 1, "actor_id": 2, "from_scene_id": 2, "to_scene_id": 5,
             "exit_side": "L", "position_id": None, "dresser_id": None, "start_sec": None,
             "locked": 0, "needs_review": 0, "note": ""},
        ],
    }


def test_seven_scene_single_copy_no_overlap():
    st = _state_seven_scene_single_copy()
    sched = scheduler.compute_schedule(st)
    don_a = next(a for a in sched["actions"]
                 if a["task_id"] == 1 and a["kind"] == "don" and a["item_id"] == 2)
    don_b = next(a for a in sched["actions"]
                 if a["task_id"] == 2 and a["kind"] == "don" and a["item_id"] == 2)
    assert (don_a["start"], don_a["end"]) == (286, 291), \
        f"甲的 I2 穿上应为 [286,291)，实得 [{don_a['start']},{don_a['end']})"
    assert don_b["start"] >= don_a["end"], \
        f"I2 重复分配：乙 {don_b['start']} 拿到副本时甲占用至 {don_a['end']}"
    # 同一副本的实际占用区间必须无交叠
    assert scheduler._find_copy_overlaps(sched["copies"]) == [], \
        f"最终排程仍存在副本交叠：{scheduler._find_copy_overlaps(sched['copies'])}"
    ivs = sorted(iv for cp in sched["copies"][2] for iv in cp)
    for x, y in zip(ivs, ivs[1:]):
        assert y[0] >= x[1], f"I2 占用区间交叠：{x} 与 {y}"
    # 无法执行的情形必须报告，而不是静默重复分配
    assert any(c["task_id"] == 2 and c["type"] in ("item", "late")
               for c in sched["conflicts"]), "乙的缺件/超时应被报告"


if __name__ == "__main__":
    print("回归测试：")
    check("锁定任务的普通更新被拒绝（start_sec/position_id）", test_locked_update_rejected)
    check("未来锁定任务不挤占更早任务（不误报超时）", test_future_locked_does_not_block_early)
    check("单件服装穿着期间不重复分配（有脱下记录）", test_single_copy_busy_until_doff)
    check("单件服装穿着期间不重复分配（无脱下记录→场次结束）",
          test_single_copy_busy_until_scene_end_without_doff)
    check("脱下被顺延 → 副本占用延长到实际脱下结束", test_delayed_doff_extends_occupancy)
    check("七场单副本反例：I2 不重复分配、最终无交叠", test_seven_scene_single_copy_no_overlap)
    print(f"全部通过（{len(PASS)} 项）")
