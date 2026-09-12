# -*- coding: utf-8 -*-
"""连排实测回放回归测试：

1. 从修订开启连排会冻结基准计划，打点/异常/补正均不改写基准方案；
2. 异常打点与补正必须留理由，补正保留原始记录；
3. 检查：时刻倒序、动作漏项、服装师并发冲突、错用服装（副本超用）；
4. 定位首个偏差并给出沿共享资源传播的等待链；
5. 多次连排汇总给出时长建议，勾选派生修订：只重排受影响任务、锁定节点不动。

运行：python3 test_rehearsal.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import db

PASS = []


def check(name, fn):
    fn()
    PASS.append(name)
    print(f"  ok - {name}")


def _fresh_db():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.DB_PATH = tmp
    db.init_db()
    con = db.connect()
    con.execute("INSERT INTO productions(id,name,created_at) VALUES(1,'T',0)")
    con.commit()
    return tmp


def _client():
    import importlib
    import app as web
    importlib.reload(web)
    return web.app.test_client()


def _base_scene(con, hat_copies=2, cloak_copies=2):
    """S1[0,100] S2[110,90] S3[220,100]；甲 S1→S2 脱斗篷戴帽，乙 S1→S3 加戴帽。"""
    con.execute("INSERT INTO scenes VALUES(1,1,1,'S1',0,100)")
    con.execute("INSERT INTO scenes VALUES(2,1,2,'S2',110,90)")
    con.execute("INSERT INTO scenes VALUES(3,1,3,'S3',220,100)")
    con.execute("INSERT INTO actors VALUES(1,1,'甲','J','L')")
    con.execute("INSERT INTO actors VALUES(2,1,'乙','Y','L')")
    con.execute("INSERT INTO dressers VALUES(1,1,'王姐')")
    con.execute("INSERT INTO items VALUES(1,1,'斗篷','costume',2,4,4,'ok',0,NULL,?)",
                (cloak_copies,))
    con.execute("INSERT INTO items VALUES(2,1,'帽子','costume',1,4,4,'ok',0,NULL,?)",
                (hat_copies,))
    # 造型：甲 S1 斗篷 / S2 帽子；乙 S1 斗篷 / S3 斗篷+帽子
    con.execute("INSERT INTO looks VALUES(1,1,1,1,'')")
    con.execute("INSERT INTO looks VALUES(2,1,1,2,'')")
    con.execute("INSERT INTO looks VALUES(3,1,2,1,'')")
    con.execute("INSERT INTO looks VALUES(4,1,2,3,'')")
    con.execute("INSERT INTO look_items VALUES(1,1,0)")
    con.execute("INSERT INTO look_items VALUES(2,2,0)")
    con.execute("INSERT INTO look_items VALUES(3,1,0)")
    con.execute("INSERT INTO look_items VALUES(4,1,0)")
    con.execute("INSERT INTO look_items VALUES(4,2,1)")
    # 任务：#1 甲 S1→S2（王姐）；#2 乙 S1→S3（王姐）
    con.execute("INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,"
                "exit_side,position_id,dresser_id,start_sec,locked) "
                "VALUES(1,1,1,2,'L',NULL,1,NULL,0)")
    con.execute("INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,"
                "exit_side,position_id,dresser_id,start_sec,locked) "
                "VALUES(1,2,1,3,'L',NULL,1,NULL,0)")
    con.commit()


def _make_run(client):
    client.post("/api/revisions", json={"note": "基准"})
    r = client.post("/api/runs", json={"revision_id": 1, "name": "连排一"})
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    return r.get_json()["id"]


def _punch(client, run_id, tid, idx, kind, at, reason=""):
    return client.post(f"/api/runs/{run_id}/events",
                       json={"task_id": tid, "action_idx": idx, "kind": kind,
                             "at_sec": at, "reason": reason})


# ---------- 用例 1：开启连排冻结基准，实测不改写基准方案 ----------

def test_run_freezes_plan_and_never_rewrites_baseline():
    tmp = _fresh_db()
    con = db.connect()
    _base_scene(con)
    con.close()
    client = _client()
    run_id = _make_run(client)

    d = client.get(f"/api/runs/{run_id}").get_json()
    # 基准计划已冻结：任务#1 计划 [100,110]，任务#2 等服装师 → [110,116]
    w = d["plan"]["windows"]
    assert (w["1"]["start"], w["1"]["end"]) == (100, 110), w["1"]
    assert (w["2"]["start"], w["2"]["end"]) == (110, 116), w["2"]

    before = db.snapshot(1)
    assert _punch(client, run_id, 1, 0, "start", 125).get_json()["ok"]
    assert _punch(client, run_id, 1, 0, "done", 126).get_json()["ok"]
    assert _punch(client, run_id, 1, 1, "exception", 128, "拉链卡住").get_json()["ok"]
    after = db.snapshot(1)
    assert before == after, "打点改写了基准方案（scenes/tasks/items 被改动）"

    # 连排计划快照也不随基准库后续改动而变化
    client.post("/api/tasks/1", json={"start_sec": 105})
    d2 = client.get(f"/api/runs/{run_id}").get_json()
    assert d2["plan"]["windows"]["1"]["start"] == 100, "连排基准快照被后续改动污染"
    os.unlink(tmp)


# ---------- 用例 2：异常/补正必须留理由，补正保留原记录 ----------

def test_reason_required_and_correction_keeps_trail():
    tmp = _fresh_db()
    con = db.connect()
    _base_scene(con)
    con.close()
    client = _client()
    run_id = _make_run(client)

    r = _punch(client, run_id, 1, 0, "exception", 105)
    assert r.status_code == 400, "异常打点无理由应被拒绝"
    r = _punch(client, run_id, 1, 0, "start", 100)
    assert r.get_json()["ok"]
    r = _punch(client, run_id, 1, 0, "start", 103)  # 补正无理由
    assert r.status_code == 400, "补正无理由应被拒绝"
    r = _punch(client, run_id, 1, 0, "start", 103, "按表误差，以场记表为准")
    assert r.get_json()["ok"]

    d = client.get(f"/api/runs/{run_id}").get_json()
    evs = [e for e in d["events"] if e["kind"] == "start"]
    assert len(evs) == 2, "补正应保留原打点记录"
    old = next(e for e in evs if e["at_sec"] == 100)
    new = next(e for e in evs if e["at_sec"] == 103)
    assert new["supersedes"] == old["id"], "补正未链接被替代事件"
    # 有效打点以补正后为准
    assert d["analysis"]["actuals"]["1:0"]["start"] == 103

    # 连排结束后禁止再打点
    client.post(f"/api/runs/{run_id}/close")
    r = _punch(client, run_id, 1, 1, "start", 110)
    assert r.status_code == 409, "已结束连排不应再接受打点"
    os.unlink(tmp)


# ---------- 用例 3：时刻倒序与动作漏项 ----------

def test_order_and_missing_checks():
    tmp = _fresh_db()
    con = db.connect()
    _base_scene(con)
    con.close()
    client = _client()
    run_id = _make_run(client)

    _punch(client, run_id, 1, 0, "start", 105)
    _punch(client, run_id, 1, 0, "done", 103)      # 完成早于开始 → 倒序
    _punch(client, run_id, 1, 1, "start", 102)     # 早于上一动作完成 → 倒序
    client.post(f"/api/runs/{run_id}/close")        # 结束连排 → 其余动作漏打点

    d = client.get(f"/api/runs/{run_id}").get_json()
    ana = d["analysis"]["anomalies"]
    order = [c for c in ana if c["type"] == "order"]
    missing = [c for c in ana if c["type"] == "missing"]
    assert len(order) >= 2, f"应检出两类时刻倒序：{order}"
    assert any("完成" in c["message"] and "早于开始" in c["message"] for c in order)
    assert any(c["task_id"] == 1 and "漏打点" in c["message"] for c in missing), \
        "结束连排后未打点的动作应记漏项"
    assert any(c["task_id"] == 2 for c in missing), "任务#2 完全未打点应记漏项"
    os.unlink(tmp)


# ---------- 用例 4：服装师并发冲突 + 首个偏差与等待链 ----------

def test_concurrency_first_deviation_and_chain():
    tmp = _fresh_db()
    con = db.connect()
    _base_scene(con)
    con.close()
    client = _client()
    run_id = _make_run(client)

    # 任务#1 实测整体晚 25s：[125,135]；任务#2 等不及王姐，133 就开工（区间交叠）
    seq1 = [(0, 125, 126), (1, 126, 130), (2, 130, 134), (3, 134, 135)]
    for idx, s, e in seq1:
        _punch(client, run_id, 1, idx, "start", s)
        _punch(client, run_id, 1, idx, "done", e)
    seq2 = [(0, 133, 134), (1, 134, 138), (2, 138, 139)]
    for idx, s, e in seq2:
        _punch(client, run_id, 2, idx, "start", s)
        _punch(client, run_id, 2, idx, "done", e)

    d = client.get(f"/api/runs/{run_id}").get_json()
    ana = d["analysis"]
    conc = [c for c in ana["anomalies"]
            if c["type"] == "concurrency" and "王姐" in c["message"]]
    assert conc, f"应检出服装师实测并发冲突：{ana['anomalies']}"

    fd = ana["first_deviation"]
    assert fd and fd["task_id"] == 1 and fd["action_idx"] == 0, \
        f"首个偏差应为任务#1 首个动作：{fd}"
    assert fd["delay"] == 25, f"偏差量应为 +25s：{fd}"
    chain_text = " ".join(c["message"] for c in ana["chain"])
    assert "同任务顺延" in chain_text, f"等待链应含同任务顺延：{chain_text}"
    assert "王姐" in chain_text and "任务#2" in chain_text, \
        f"等待链应传播到等待服装师的任务#2：{chain_text}"
    os.unlink(tmp)


# ---------- 用例 5：错用服装（单副本实测并发穿着） ----------

def test_item_copy_misuse_detected():
    tmp = _fresh_db()
    con = db.connect()
    # 斗篷仅 1 件：甲 S1→S2 穿上，S2→S3 脱下；乙 S2→S3 也要穿
    con.execute("INSERT INTO scenes VALUES(1,1,1,'S1',0,100)")
    con.execute("INSERT INTO scenes VALUES(2,1,2,'S2',110,90)")
    con.execute("INSERT INTO scenes VALUES(3,1,3,'S3',220,100)")
    con.execute("INSERT INTO actors VALUES(1,1,'甲','J','L')")
    con.execute("INSERT INTO actors VALUES(2,1,'乙','Y','L')")
    con.execute("INSERT INTO items VALUES(1,1,'斗篷','costume',2,4,4,'ok',0,NULL,1)")
    for lid, aid, sid in ((1, 1, 1), (2, 1, 2), (3, 1, 3), (4, 2, 2), (5, 2, 3)):
        con.execute("INSERT INTO looks(id,production_id,actor_id,scene_id,name) "
                    "VALUES(?,1,?,?,'')", (lid, aid, sid))
    con.execute("INSERT INTO look_items VALUES(2,1,0)")   # 甲 S2 穿斗篷
    con.execute("INSERT INTO look_items VALUES(5,1,0)")   # 乙 S3 穿斗篷
    con.execute("INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,"
                "exit_side) VALUES(1,1,1,2,'L')")          # #1 甲穿上
    con.execute("INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,"
                "exit_side) VALUES(1,1,2,3,'L')")          # #2 甲脱下
    con.execute("INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,"
                "exit_side) VALUES(1,2,2,3,'L')")          # #3 乙穿上
    con.commit()
    con.close()
    client = _client()
    run_id = _make_run(client)

    # 甲 101 穿上；乙在甲脱下完成(210)之前 201 就穿上 → 单副本实测并发
    for tid, idx, s, e in [(1, 0, 100, 101), (1, 1, 101, 105), (1, 2, 105, 106),
                           (2, 0, 200, 201), (2, 1, 201, 210), (2, 2, 210, 211),
                           (3, 0, 200, 201), (3, 1, 201, 205), (3, 2, 205, 206)]:
        _punch(client, run_id, tid, idx, "start", s)
        _punch(client, run_id, tid, idx, "done", e)

    d = client.get(f"/api/runs/{run_id}").get_json()
    misuse = [c for c in d["analysis"]["anomalies"] if c["type"] == "item"]
    assert any("斗篷" in c["message"] and "副本" in c["message"] for c in misuse), \
        f"应检出错用服装（单副本并发穿着）：{d['analysis']['anomalies']}"
    os.unlink(tmp)


# ---------- 用例 6：汇总建议与派生修订（锁定节点不动） ----------

def test_summary_and_derive_respects_locks():
    tmp = _fresh_db()
    con = db.connect()
    _base_scene(con)
    con.close()
    client = _client()

    # 两轮连排：帽子穿上实测 6/7s 与 5/6s（基准 4s）
    for run_no, durs in ((1, (6, 7)), (2, (5, 6))):
        run_id = _make_run(client)
        d1, d2 = durs
        _punch(client, run_id, 1, 2, "start", 100)
        _punch(client, run_id, 1, 2, "done", 100 + d1)   # 甲 穿·帽子
        _punch(client, run_id, 2, 1, "start", 200)
        _punch(client, run_id, 2, 1, "done", 200 + d2)   # 乙 穿·帽子
        client.post(f"/api/runs/{run_id}/close")

    r = client.get("/api/runs/summary").get_json()
    sugg = [s for s in r["suggestions"] if s["item_name"] == "帽子" and s["action"] == "don"]
    assert sugg, f"应给出帽子穿上时长建议：{r}"
    s = sugg[0]
    assert s["current"] == 4 and s["suggested"] == 6 and s["n"] == 4, s

    # 锁定任务#2（固定 110 开始）后派生：建议生效、#2 不动、生成新修订
    client.post("/api/tasks/2", json={"start_sec": 110, "locked": 1})
    r = client.post("/api/runs/derive", json={"keys": [s["key"]]}).get_json()
    assert "derived" in r, r
    con = db.connect()
    hat = con.execute("SELECT don_sec FROM items WHERE id=2").fetchone()["don_sec"]
    t2 = con.execute("SELECT start_sec, locked FROM tasks WHERE id=2").fetchone()
    revs = con.execute("SELECT COUNT(*) c FROM revisions").fetchone()["c"]
    con.close()
    assert hat == 6, f"建议未写回服装用时：{hat}"
    assert t2["start_sec"] == 110 and t2["locked"] == 1, "锁定节点被派生重排移动"
    assert revs >= 2, "派生未生成新修订"
    os.unlink(tmp)


if __name__ == "__main__":
    print("连排实测回归测试：")
    check("开启连排冻结基准，实测不改写基准方案", test_run_freezes_plan_and_never_rewrites_baseline)
    check("异常/补正必须留理由，补正保留原记录", test_reason_required_and_correction_keeps_trail)
    check("时刻倒序与动作漏项检查", test_order_and_missing_checks)
    check("服装师并发冲突 + 首个偏差与等待链", test_concurrency_first_deviation_and_chain)
    check("错用服装（单副本实测并发穿着）", test_item_copy_misuse_detected)
    check("汇总建议与派生修订（锁定节点不动）", test_summary_and_derive_respects_locks)
    print(f"全部通过（{len(PASS)} 项）")
