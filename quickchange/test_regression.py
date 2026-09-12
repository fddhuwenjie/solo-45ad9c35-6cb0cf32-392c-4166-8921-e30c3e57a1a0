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


if __name__ == "__main__":
    print("回归测试：")
    check("锁定任务的普通更新被拒绝（start_sec/position_id）", test_locked_update_rejected)
    check("未来锁定任务不挤占更早任务（不误报超时）", test_future_locked_does_not_block_early)
    check("单件服装穿着期间不重复分配（有脱下记录）", test_single_copy_busy_until_doff)
    check("单件服装穿着期间不重复分配（无脱下记录→场次结束）",
          test_single_copy_busy_until_scene_end_without_doff)
    print(f"全部通过（{len(PASS)} 项）")
