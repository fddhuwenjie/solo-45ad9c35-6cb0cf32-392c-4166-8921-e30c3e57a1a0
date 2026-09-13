# -*- coding: utf-8 -*-
"""动作级协作回归测试：

1. 跨侧台步行计入到岗约束：左侧动作 105 秒结束、右侧锁定动作 107 秒开始、
   跨越需 28 秒时，必须定位最早到岗冲突（最早 02:13）并给出可直接采用的
   换人替代分工；解锁后自动顺延到 133；
2. 服装师技能/不可用时段变化只标记真正受影响的具体动作待复核；
3. 双人动作提交空实际参与者列表按「无人参与」保存，回放触发人数不足异常。

运行：python3 test_collab.py
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


def cross_state(locked):
    """左动作（王姐）105 结束；右动作锁定 107 开始（固定时段），同由王姐。
    换装位就在两侧上场口，同侧 walk=1s，跨侧步行=28s。"""
    return {
        "scenes": [
            {"id": 1, "production_id": 1, "seq": 1, "name": "S1",
             "start_sec": 0, "duration_sec": 100},
            {"id": 2, "production_id": 1, "seq": 2, "name": "S2",
             "start_sec": 145, "duration_sec": 100},
        ],
        "actors": [
            {"id": 1, "production_id": 1, "name": "甲", "code": "", "default_side": "L"},
            {"id": 2, "production_id": 1, "name": "乙", "code": "", "default_side": "R"},
        ],
        "items": [
            {"id": 1, "production_id": 1, "name": "束身衣", "kind": "costume",
             "layer": 1, "don_sec": 6, "doff_sec": 4, "status": "ok",
             "available_at": 0, "cart_id": None, "copies": 3, "skill_id": None},
        ],
        "looks": [
            {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
            {"id": 2, "production_id": 1, "actor_id": 2, "scene_id": 2, "name": ""},
        ],
        "look_items": [
            {"look_id": 1, "item_id": 1, "ord": 0},
            {"look_id": 2, "item_id": 1, "ord": 0},
        ],
        "dressers": [
            {"id": 1, "production_id": 1, "name": "王姐"},
            {"id": 2, "production_id": 1, "name": "小李"},
        ],
        "positions": [
            {"id": 1, "production_id": 1, "name": "左口位", "side": "L",
             "x": 2, "y": 10, "capacity": 2},
            {"id": 2, "production_id": 1, "name": "右口位", "side": "R",
             "x": 38, "y": 10, "capacity": 2},
        ],
        "carts": [],
        "tasks": [
            {"id": 1, "production_id": 1, "actor_id": 1, "from_scene_id": 1,
             "to_scene_id": 2, "exit_side": "L", "position_id": 1,
             "dresser_id": None, "start_sec": None, "locked": 0,
             "needs_review": 0, "note": ""},
            {"id": 2, "production_id": 1, "actor_id": 2, "from_scene_id": 1,
             "to_scene_id": 2, "exit_side": "R", "position_id": 2,
             "dresser_id": None, "start_sec": 107,
             "locked": 1 if locked else 0, "needs_review": 0, "note": ""},
        ],
        "skills": [], "dresser_skills": [], "dresser_sides": [],
        "dresser_unavailable": [], "action_specs": [],
        "action_staff": [
            {"id": 1, "production_id": 1, "task_id": 1, "kind": "doff",
             "item_id": 1, "seq": 0, "dresser_id": 1, "is_lead": 1,
             "locked": 0, "created_at": 0},
            {"id": 2, "production_id": 1, "task_id": 2, "kind": "don",
             "item_id": 1, "seq": 0, "dresser_id": 1, "is_lead": 1,
             "locked": 0, "created_at": 0},
        ],
        "action_reviews": [],
    }


# ---------- 用例 1：跨侧台步行 28s —— 锁定报冲突+换人；解锁顺延 ----------

def test_cross_side_walk_28s():
    # 纯函数：两侧上场口之间正好 28s；绕口路径恒 ≥28s
    assert scheduler.transfer_walk(scheduler.EXITS["L"], "L",
                                   scheduler.EXITS["R"], "R") == 28
    assert scheduler.transfer_walk((6, 4), "L", (34, 6), "R") > \
        scheduler.walk_sec((6, 4), (34, 6))   # 不能穿台直线

    st = cross_state(locked=True)
    r = scheduler.compute_schedule(st)
    doff = next(a for a in r["actions"] if a["task_id"] == 1 and a["kind"] == "doff")
    assert doff["end"] == 105, doff
    arrivals = [c for c in r["conflicts"]
                if c["task_id"] == 2 and c["type"] == "arrival"]
    assert arrivals, "必须报告跨侧到岗过晚冲突"
    msg = arrivals[0]["message"]
    assert "跨侧台步行 28s" in msg and "02:13" in msg, msg

    # 最少改动替代分工：换右侧空闲的小李
    sg = scheduler.suggest(st)
    swap = [s for s in sg if s["task_id"] == 2 and s["staff_changes"]]
    assert swap, f"应给出换人建议：{sg}"
    assert swap[0]["changes"] == {}, "纯换人不应携带时刻改动"
    assert swap[0]["staff_changes"][0]["dresser_ids"] == [2]
    fixed = scheduler.compute_schedule(
        st, overrides={"__staff__": {(2, "don", 1, 0): [2]}})
    assert fixed["windows"][2]["ok"], fixed["windows"][2]
    assert not [c for c in fixed["conflicts"] if c["task_id"] == 2]

    # 解锁：自动把右动作顺延到 105+28=133，窗口按时（根因提示保留说明顺延原因）
    r2 = scheduler.compute_schedule(cross_state(locked=False))
    don2 = next(a for a in r2["actions"]
                if a["task_id"] == 2 and a["kind"] == "don")
    assert don2["start"] == 133, don2
    assert r2["windows"][2]["ok"], r2["windows"][2]


# ---------- 用例 2：资料/不可用变化只标记真正受影响动作（API 级） ----------

def _fresh_client():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.DB_PATH = tmp
    db.init_db()
    con = db.connect()
    con.execute("INSERT INTO productions(id,name,created_at) VALUES(1,'T',0)")
    con.execute("INSERT INTO scenes VALUES(1,1,1,'S1',0,100)")
    con.execute("INSERT INTO scenes VALUES(2,1,2,'S2',110,90)")
    con.execute("INSERT INTO actors VALUES(1,1,'甲','J','L')")
    con.execute("INSERT INTO dressers VALUES(1,1,'王姐')")
    con.execute("INSERT INTO skills VALUES(1,1,'紧身衣')")
    con.execute("INSERT INTO items(id,production_id,name,kind,layer,don_sec,doff_sec,"
                "status,available_at,cart_id,copies,skill_id) "
                "VALUES(1,1,'紧身衣','costume',1,4,4,'ok',0,NULL,2,1)")
    con.execute("INSERT INTO items(id,production_id,name,kind,layer,don_sec,doff_sec,"
                "status,available_at,cart_id,copies,skill_id) "
                "VALUES(2,1,'帽子','costume',1,4,4,'ok',0,NULL,2,NULL)")
    con.execute("INSERT INTO looks VALUES(1,1,1,2,'')")
    con.execute("INSERT INTO look_items VALUES(1,1,0)")
    con.execute("INSERT INTO look_items VALUES(1,2,1)")
    con.execute("INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,"
                "exit_side) VALUES(1,1,1,2,'L')")
    # 王姐同时负责需技能的紧身衣与无技能要求的帽子
    con.execute("INSERT INTO action_staff(production_id,task_id,kind,item_id,seq,"
                "dresser_id,is_lead,locked,created_at) VALUES(1,1,'don',1,0,1,1,0,0)")
    con.execute("INSERT INTO action_staff(production_id,task_id,kind,item_id,seq,"
                "dresser_id,is_lead,locked,created_at) VALUES(1,1,'don',2,0,1,1,0,0)")
    con.commit()
    con.close()
    import importlib
    import app as web
    importlib.reload(web)
    return web.app.test_client(), tmp


def test_profile_change_only_marks_affected_actions():
    client, tmp = _fresh_client()
    # 初始具备技能：两动作合格、无待复核
    client.post("/api/dressers/1/profile", json={"skill_ids": [1]})
    assert client.get("/api/state").get_json()["action_reviews"] == []
    # 失去技能：只有紧身衣动作待复核，帽子不受影响
    client.post("/api/dressers/1/profile", json={"skill_ids": []})
    rv = [(r["kind"], r["item_id"])
          for r in client.get("/api/state").get_json()["action_reviews"]]
    assert rv == [("don", 1)], rv
    # 重复提交相同资料不产生新标记
    client.post("/api/tasks/1/clear_review",
                json={"kind": "don", "item_id": 1, "seq": 0})
    client.post("/api/dressers/1/profile", json={"skill_ids": []})
    assert client.get("/api/state").get_json()["action_reviews"] == []
    # 恢复技能不标复核（获得资格不阻断计划）
    client.post("/api/dressers/1/profile", json={"skill_ids": [1]})
    assert client.get("/api/state").get_json()["action_reviews"] == []
    os.unlink(tmp)


def test_unavailable_only_marks_overlapping_actions():
    client, tmp = _fresh_client()
    # 紧身衣 don 在约 101–105 秒；500–600 的不可用不影响 → 不标记
    r = client.post("/api/dresser_unavailable",
                    json={"dresser_id": 1, "start_sec": 500, "end_sec": 600,
                          "reason": "换位"})
    assert r.get_json()["ok"]
    assert client.get("/api/state").get_json()["action_reviews"] == []
    # 与动作时间相交的不可用时段才标记
    client.post("/api/dresser_unavailable",
                json={"dresser_id": 1, "start_sec": 100, "end_sec": 110})
    rv = [(r["kind"], r["item_id"])
          for r in client.get("/api/state").get_json()["action_reviews"]]
    assert ("don", 1) in rv, rv
    # 非法时段（结束≤开始）拒绝
    bad = client.post("/api/dresser_unavailable",
                      json={"dresser_id": 1, "start_sec": 10, "end_sec": 5})
    assert bad.status_code == 400, bad.status_code
    os.unlink(tmp)


# ---------- 用例 3：空实际参与者 → 人数不足异常 ----------

def test_empty_participants_understaffed():
    client, tmp = _fresh_client()
    con = db.connect()
    # 紧身衣动作要求 2 人，登记王姐（赵妈待建）
    con.execute("INSERT OR IGNORE INTO dressers VALUES(2,1,'赵妈')")
    con.execute("INSERT INTO action_specs(production_id,task_id,kind,item_id,seq,"
                "skill_id,required_count) VALUES(1,1,'don',1,0,1,2)")
    con.execute("INSERT INTO action_staff(production_id,task_id,kind,item_id,seq,"
                "dresser_id,is_lead,locked,created_at) VALUES(1,1,'don',1,0,2,0,0,0)")
    con.commit()
    con.close()
    client.post("/api/revisions", json={"note": "基准"})
    rid = client.post("/api/runs", json={"revision_id": 1}).get_json()["id"]
    plan = client.get(f"/api/runs/{rid}").get_json()["plan"]
    don = next(a for a in plan["actions"] if a["kind"] == "don" and a["item_id"] == 1)
    assert don["required"] == 2 and sorted(don["staff_ids"]) == [1, 2]
    # 显式空参与者：按「无人参与」保存（不回退冻结分工）
    r = client.post(f"/api/runs/{rid}/events",
                    json={"task_id": 1, "action_idx": don["idx"], "kind": "start",
                          "at_sec": 102, "dresser_ids": []})
    assert r.status_code == 200 and r.get_json()["ok"], r.get_data(as_text=True)
    client.post(f"/api/runs/{rid}/events",
                json={"task_id": 1, "action_idx": don["idx"], "kind": "done",
                      "at_sec": 106, "dresser_ids": []})
    anomalies = client.get(f"/api/runs/{rid}").get_json()["analysis"]["anomalies"]
    under = [a for a in anomalies if a["type"] == "staff"]
    assert any("需 2 人" in a["message"] and "0 人" in a["message"] for a in under), \
        anomalies
    # 对照：两人正常参与不报人数不足
    rid2 = client.post("/api/runs", json={"revision_id": 1}).get_json()["id"]
    client.post(f"/api/runs/{rid2}/events",
                json={"task_id": 1, "action_idx": don["idx"], "kind": "start",
                      "at_sec": 102, "dresser_ids": [1, 2]})
    client.post(f"/api/runs/{rid2}/events",
                json={"task_id": 1, "action_idx": don["idx"], "kind": "done",
                      "at_sec": 106, "dresser_ids": [1, 2]})
    ana2 = client.get(f"/api/runs/{rid2}").get_json()["analysis"]["anomalies"]
    assert not [a for a in ana2 if a["type"] == "staff"], ana2
    os.unlink(tmp)


if __name__ == "__main__":
    print("动作级协作回归测试：")
    check("跨侧台步行28s：锁定报最早到岗冲突+换人建议可采用；解锁顺延到133",
          test_cross_side_walk_28s)
    check("技能变化只标记失去资格的具体动作待复核",
          test_profile_change_only_marks_affected_actions)
    check("不可用时段只标记时间相交的动作；非法时段拒绝",
          test_unavailable_only_marks_overlapping_actions)
    check("空实际参与者按无人参与保存并触发人数不足异常",
          test_empty_participants_understaffed)
    print(f"全部通过（{len(PASS)} 项）")
