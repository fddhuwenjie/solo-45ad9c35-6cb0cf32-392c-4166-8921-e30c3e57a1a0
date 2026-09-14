# -*- coding: utf-8 -*-
"""场间复位工作区回归测试：

1. 从连排实际穿用记录/已确认方案生成逐副本路线与就位期限，道具不成路线；
2. 排程约束：步骤次序、设备容量、人员技能、烘干冷却间隔、晚场就位期限；
3. 无法按时归位标出最早卡点，并给出晚场引用它的换装任务；
4. 完工锁定后不再挪动，人员/工位改派只重算后续；
5. 退回重做使工序及后序回到待排；报废取消后序并提醒核对引用任务；
6. 归档随附来源记录、人工计划与执行事件，归档后只读；
7. 异常打点「开线」自动加入缝补工序。

运行：python3 test_turnaround.py
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


# 日场 S1[0,200] S2[400,200] S3[3200,200]（S3 模拟晚场，由偏移平移）；
# 养护窗口 200–3200：约 50 分钟，足够完整链条。
def _base(con, copies=2, item_status="ok", skill_needed=False):
    con.execute("INSERT INTO scenes VALUES(1,1,1,'S1',0,200)")
    con.execute("INSERT INTO scenes VALUES(2,1,2,'S2',400,200)")
    con.execute("INSERT INTO scenes VALUES(3,1,3,'晚场',3200,200)")
    con.execute("INSERT INTO actors VALUES(1,1,'甲','J','L')")
    con.execute("INSERT INTO actors VALUES(2,1,'乙','Y','L')")
    con.execute("INSERT INTO dressers VALUES(1,1,'王姐')")
    con.execute("INSERT INTO dressers VALUES(2,1,'小李')")
    con.execute("INSERT INTO items(id,production_id,name,kind,layer,don_sec,doff_sec,"
                "status,available_at,cart_id,copies) VALUES(1,1,'礼服','costume',2,4,4,?,"
                "0,NULL,?)", (item_status, copies))
    con.execute("INSERT INTO items(id,production_id,name,kind,layer,don_sec,doff_sec,"
                "status,available_at,cart_id,copies) VALUES(2,1,'船票','prop',0,0,0,'ok',0,NULL,1)")
    # 造型：甲乙 S1 都穿礼服；甲 S2 脱下（任务#1），乙 S3 晚场再穿（任务#3）
    for lid, aid, sid in ((1, 1, 1), (2, 2, 1), (3, 1, 2), (4, 2, 3)):
        con.execute("INSERT INTO looks VALUES(?,1,?,?,'')", (lid, aid, sid))
    con.execute("INSERT INTO look_items VALUES(1,1,0)")
    con.execute("INSERT INTO look_items VALUES(2,1,0)")
    con.execute("INSERT INTO look_items VALUES(4,1,0)")
    con.execute("INSERT INTO look_items VALUES(4,2,1)")
    # 任务 #1 甲 S1→S2 脱礼服；#2 乙 S1→S2（脱）；#3 乙 S2→S3 穿礼服（晚场需求）
    for tid, aid, f, t in ((1, 1, 1, 2), (2, 2, 1, 2), (3, 2, 2, 3)):
        con.execute("INSERT INTO tasks(id,production_id,actor_id,from_scene_id,to_scene_id,"
                    "exit_side,position_id,dresser_id,start_sec,locked) "
                    "VALUES(?,1,?,?,?,'L',NULL,1,NULL,0)", (tid, aid, f, t))
    con.commit()
    if skill_needed:
        con.execute("INSERT INTO skills VALUES(1,1,'整烫证')")
        # 只给小李（id=2）
        con.execute("INSERT INTO dresser_skills VALUES(2,1)")
        con.commit()


def _resources(con, clean_cap=1, dry_cd=0):
    rows = [
        ("清洁机", "clean", 0, clean_cap, 0, None),
        ("烘干机", "dry", 0, 1, dry_cd, None),
        ("缝补台", "mend", 1, 2, 0, None),
        ("整烫台", "press", 1, 1, turnaround.DRY_COOL_DOWN if False else 0, None),
        ("装车口", "load", 1, 3, 0, None),
    ]
    for name, kind, st, cap, cd, sk in rows:
        con.execute("INSERT INTO care_resources(production_id,name,kind,is_station,capacity,"
                    "cool_down_sec,skill_id) VALUES(1,?,?,?,?,?,?)",
                    (name, kind, st, cap, cd, sk))
    con.commit()


import turnaround  # noqa: E402


def _make_run(client, with_exception=None):
    client.post("/api/revisions", json={"note": "基准"})
    r = client.post("/api/runs", json={"revision_id": 1, "name": "日场连排"})
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    run_id = r.get_json()["id"]
    d = client.get(f"/api/runs/{run_id}").get_json()
    # 找到甲任务#1 与乙任务#2 的脱礼动作（doff item1）打点
    def punch_doff(tid, end, exc=None):
        acts = [a for a in d["plan"]["actions"]
                if a["task_id"] == tid and a["kind"] == "doff" and a["item_id"] == 1]
        assert acts, f"task {tid} 无脱礼动作"
        a = acts[0]
        client.post(f"/api/runs/{run_id}/events",
                    json={"task_id": tid, "action_idx": a["idx"], "kind": "start",
                          "at_sec": end - a["dur"]})
        if exc:
            client.post(f"/api/runs/{run_id}/events",
                        json={"task_id": tid, "action_idx": a["idx"], "kind": "exception",
                              "at_sec": end - 1, "reason": exc})
        rr = client.post(f"/api/runs/{run_id}/events",
                         json={"task_id": tid, "action_idx": a["idx"], "kind": "done",
                               "at_sec": end})
        assert rr.get_json()["ok"], rr.get_json()
    punch_doff(1, 210, with_exception)
    punch_doff(2, 212)
    client.post(f"/api/runs/{run_id}/close")
    return run_id


def _open_turnaround(client, run_id, offset=3000):
    r = client.post("/api/turnarounds", json={
        "source_kind": "run", "source_id": run_id,
        "evening_offset_sec": offset, "name": "日→晚复位"})
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    return r.get_json()["id"], r.get_json()["detail"]


# ---------- 用例 1：路线生成与晚场期限 ----------

def test_routes_generated_with_deadline():
    _fresh_db()
    con = db.connect()
    _base(con, copies=2)
    con.close()
    client = _client()
    run_id = _make_run(client)
    tid, d = _open_turnaround(client, run_id)
    routes = d["routes"]
    # 甲、乙各脱下第 1、2 件（副本由基准日历分配），共两条路线，道具不成路线
    assert len(routes) == 2, [(r["item_name"], r["copy_no"]) for r in routes]
    nos = sorted(r["copy_no"] for r in routes)
    assert nos == [1, 2]
    # 默认工序链 clean→dry→press→load（无维修/异常）
    kinds = [[s["kind"] for s in d["steps"] if s["route_id"] == routes[0]["id"]]]
    assert kinds == [["clean", "dry", "press", "load"]], kinds
    # 晚场只有乙的任务#3 引用其中一件：被引用件 deadline=偏移后任务#3窗口开始，
    # 另一件晚场不再引用（deadline 为空）
    deadlines = sorted((r["deadline_sec"] is not None) for r in routes)
    assert deadlines == [False, True]
    used = next(r for r in routes if r["deadline_sec"] is not None)
    assert 3 in used["ref_task_ids"], used["ref_task_ids"]
    assert used["deadline_sec"] >= 3000


def test_generate_from_revision():
    _fresh_db()
    con = db.connect()
    _base(con, copies=2)
    _resources(con)
    con.close()
    client = _client()
    client.post("/api/revisions", json={"note": "确认版"})
    r = client.post("/api/turnarounds", json={
        "source_kind": "revision", "source_id": 1, "evening_offset_sec": 3000})
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    d = r.get_json()["detail"]
    assert len(d["routes"]) == 2
    assert d["source"]["kind"] == "revision"


def test_mend_step_from_exception_keyword():
    _fresh_db()
    con = db.connect()
    _base(con, copies=2)
    con.close()
    client = _client()
    run_id = _make_run(client, with_exception="礼服开线两寸")
    tid, d = _open_turnaround(client, run_id)
    # 异常来自甲任务#1（其脱下的那件）→ 该路线含 mend
    routes = {r["id"]: r for r in d["routes"]}
    found_mend = False
    for rid, r in routes.items():
        ks = [s["kind"] for s in d["steps"] if s["route_id"] == rid]
        if "mend" in ks:
            assert ks == ["clean", "dry", "mend", "press", "load"]
            found_mend = True
    assert found_mend, [(r["item_name"], r["copy_no"])
                        for r in d["routes"]]


# ---------- 用例 2：约束（次序/容量/技能/冷却/期限） ----------

def test_order_capacity_skill_and_cooldown():
    _fresh_db()
    con = db.connect()
    _base(con, copies=2, skill_needed=True)
    # 烘干机自带 60s 冷却；整烫台要求整烫证（仅小李具备）
    con.execute("INSERT INTO care_resources(production_id,name,kind,is_station,capacity,"
                "cool_down_sec,skill_id) VALUES(1,'清洁机','clean',0,2,0,NULL),"
                "(1,'烘干机','dry',0,1,60,NULL),"
                "(1,'缝补台','mend',1,2,0,NULL),"
                "(1,'整烫台','press',1,1,0,1),"
                "(1,'装车口','load',1,3,0,NULL)")
    con.commit()
    con.close()
    client = _client()
    run_id = _make_run(client)
    tid, d = _open_turnaround(client, run_id)
    steps = {(s["route_id"], s["kind"]): s for s in d["steps"]}
    routes = {r["id"]: r for r in d["routes"]}
    # 每条路线严格串行
    for rid in routes:
        prev_end = 0
        for kind in ["clean", "dry", "press", "load"]:
            s = steps[(rid, kind)]
            assert s["start"] >= prev_end, (kind, s["start"], prev_end)
            prev_end = s["end"]
        # 烘干冷却：press 开始 ≥ dry 结束 + DRY_COOL_DOWN
        assert steps[(rid, "press")]["start"] >= \
            steps[(rid, "dry")]["end"] + turnaround.DRY_COOL_DOWN
    # 烘干机两件不得重叠（容量1+冷却60）
    dries = sorted((steps[(rid, "dry")] for rid in routes),
                   key=lambda s: s["start"])
    assert dries[1]["start"] >= dries[0]["end"] + 60
    # 整烫必须派给小李（id=2，唯一持整烫证）
    for rid in routes:
        assert steps[(rid, "press")]["res_dresser_id"] == 2
    # 无技能冲突/资源缺失
    assert not [c for c in d["conflicts"] if c["type"] in ("skill", "no_resource")]


def test_late_deadline_earliest_blocker():
    _fresh_db()
    con = db.connect()
    _base(con, copies=1)
    # 只放一台清洁机且人为把链条拉长：把清洁用时调到很大，挤占就位期限
    _resources(con)
    con.close()
    client = _client()
    run_id = _make_run(client)
    tid, d = _open_turnaround(client, run_id, offset=3000)
    # 唯一一件被晚场任务#3引用；把清洁工序改成 4000s → 必超 deadline
    routes = d["routes"]
    used = next(r for r in routes if r["deadline_sec"] is not None)
    clean = next(s for s in d["steps"] if s["route_id"] == used["id"] and s["kind"] == "clean")
    r = client.post(f"/api/care_steps/{clean['id']}",
                    json={"dur_sec": 4000, "start_sec": used["released_at"]})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()["detail"]
    late = [c for c in d["conflicts"] if c["type"] == "late"]
    assert late, d["conflicts"]
    earliest = d["earliest"]
    assert earliest and earliest["type"] == "late"
    # 卡点消息点名晚场期限与换装任务
    assert scheduler_fmt(used["deadline_sec"]) in earliest["message"]
    used2 = next(x for x in d["routes"] if x["id"] == used["id"])
    assert used2["on_time"] is False


def scheduler_fmt(sec):
    import scheduler
    return scheduler.fmt(sec)


# ---------- 用例 3：完工锁定 / 改派只动后续 / 退回 / 报废 ----------

def _setup_with_resources(client, offset=3000, **kw):
    _fresh_db()
    con = db.connect()
    _base(con, copies=2, **kw)
    _resources(con)
    con.close()
    run_id = _make_run(client)
    return _open_turnaround(client, run_id, offset=offset)


def test_done_locked_and_reassign_only_followers():
    client = _client()
    tid, d = _setup_with_resources(client)
    routes = {r["id"]: r for r in d["routes"]}
    # 给第一条路线的 clean 打点开工+完工 → 锁定
    rid = sorted(routes)[0]
    clean = next(s for s in d["steps"] if s["route_id"] == rid and s["kind"] == "clean")
    assert client.post(f"/api/care_steps/{clean['id']}/events",
                       json={"kind": "start", "at_sec": 210}).get_json()["ok"]
    r = client.post(f"/api/care_steps/{clean['id']}/events",
                    json={"kind": "done", "at_sec": 210 + clean["dur_sec"]})
    assert r.get_json()["ok"], r.get_json()
    d = r.get_json()["detail"]
    clean2 = next(s for s in d["steps"] if s["id"] == clean["id"])
    assert clean2["locked"] and clean2["status"] == "done"
    locked_start, locked_res = clean2["start"], clean2["res_resource_id"]
    # 锁定后拒绝改时间/改派
    other_res = [x for x in db.care_resources(1) if x["kind"] == "clean"
                 and x["id"] != locked_res]
    if other_res:
        rr = client.post(f"/api/care_steps/{clean['id']}",
                         json={"resource_id": other_res[0]["id"]})
        assert rr.status_code == 409
    rr = client.post(f"/api/care_steps/{clean['id']}", json={"start_sec": 250})
    assert rr.status_code == 409
    # 改派下一工序（dry）的人员：只重算后续，clean 时段纹丝不动
    dry = next(s for s in d["steps"] if s["route_id"] == rid and s["kind"] == "dry")
    old_dresser = dry["res_dresser_id"]
    new_dr = 2 if old_dresser == 1 else 1
    r = client.post(f"/api/care_steps/{dry['id']}", json={"dresser_id": new_dr})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()["detail"]
    clean3 = next(s for s in d["steps"] if s["id"] == clean["id"])
    assert (clean3["start"], clean3["res_resource_id"]) == (locked_start, locked_res)
    dry2 = next(s for s in d["steps"] if s["id"] == dry["id"])
    assert dry2["res_dresser_id"] == new_dr


def test_return_redo_and_scrap():
    client = _client()
    tid, d = _setup_with_resources(client)
    rid = sorted({r["id"] for r in d["routes"]})[0]
    chain = [s for s in sorted((x for x in d["steps"] if x["route_id"] == rid),
                               key=lambda x: x["seq"])]
    clean, dry = chain[0], chain[1]
    # clean 完工后 dry 开工；随后 clean 退回重做 → clean/dry 回到待排
    client.post(f"/api/care_steps/{clean['id']}/events",
                json={"kind": "done", "at_sec": clean["start"] + clean["dur_sec"]})
    client.post(f"/api/care_steps/{dry['id']}/events",
                json={"kind": "start", "at_sec": dry["start"]})
    r = client.post(f"/api/care_steps/{clean['id']}/events",
                    json={"kind": "return", "at_sec": 700, "reason": "污渍未净"})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()["detail"]
    c2 = next(s for s in d["steps"] if s["id"] == clean["id"])
    assert c2["returned"] and c2["status"] == "planned"
    assert next(s for s in d["steps"] if s["id"] == dry["id"])["status"] == "planned"
    # 报废必须留理由
    r = client.post(f"/api/care_steps/{clean['id']}/events",
                    json={"kind": "scrap", "at_sec": 710})
    assert r.status_code == 400
    r = client.post(f"/api/care_steps/{clean['id']}/events",
                    json={"kind": "scrap", "at_sec": 710, "reason": "撕裂不可修复"})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()["detail"]
    route = next(x for x in d["routes"] if x["id"] == rid)
    assert route["status"] == "scrapped"
    scrap = [c for c in d["conflicts"] if c["type"] == "scrap"]
    assert scrap and "重新核对晚场引用" in scrap[0]["message"]
    # 报废后拒绝再登记事件
    rr = client.post(f"/api/care_steps/{dry['id']}/events",
                     json={"kind": "start", "at_sec": 720})
    assert rr.status_code == 409


def test_wrong_resource_kind_rejected():
    client = _client()
    tid, d = _setup_with_resources(client)
    clean = next(s for s in d["steps"] if s["kind"] == "clean")
    press = next(r for r in db.care_resources(1) if r["kind"] == "press")
    r = client.post(f"/api/care_steps/{clean['id']}",
                    json={"resource_id": press["id"]})
    assert r.status_code == 400


# ---------- 用例 4：归档 ----------

def test_archive_pack_and_readonly():
    client = _client()
    tid, d = _setup_with_resources(client)
    rid = d["routes"][0]["id"]
    clean = next(s for s in d["steps"] if s["route_id"] == rid and s["kind"] == "clean")
    client.post(f"/api/care_steps/{clean['id']}/events",
                json={"kind": "start", "at_sec": clean["start"]})
    r = client.post(f"/api/turnarounds/{tid}/archive")
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    tr = db.get_turnaround(tid)
    assert tr["status"] == "archived" and tr["archived_json"]
    import json
    pack = json.loads(tr["archived_json"])
    assert pack["source"]["kind"] == "run"
    assert pack["steps"] and pack["events"]
    # 导出可打印记录
    assert client.get(f"/export/turnarounds/{tid}/record").status_code == 200
    # 归档后只读
    rr = client.post(f"/api/care_steps/{clean['id']}", json={"note": "x"})
    assert rr.status_code == 409
    rr = client.post(f"/api/care_steps/{clean['id']}/events",
                     json={"kind": "done", "at_sec": 900})
    assert rr.status_code == 409


def run():
    check("路线生成+晚场就位期限", test_routes_generated_with_deadline)
    check("从已确认方案（修订）生成", test_generate_from_revision)
    check("异常开线触发缝补工序", test_mend_step_from_exception_keyword)
    check("次序/容量/技能/冷却约束", test_order_capacity_skill_and_cooldown)
    check("超期标出最早卡点与引用任务", test_late_deadline_earliest_blocker)
    check("完工锁定与改派只动后续", test_done_locked_and_reassign_only_followers)
    check("退回重做与报废提醒", test_return_redo_and_scrap)
    check("资源类型不匹配拒绝", test_wrong_resource_kind_rejected)
    check("归档包与只读", test_archive_pack_and_readonly)
    print(f"\n{len(PASS)} 个场间复位用例全部通过 ✓")


if __name__ == "__main__":
    run()
