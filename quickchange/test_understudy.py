# -*- coding: utf-8 -*-
"""替演推演回归测试：

1. 候补沿用原角色造型（不需另建个人造型）；合身时偏差/闭合件调整穿脱时长；
2. 尺寸缺失、适配越界硬冲突并定位最早；边界尺寸、人工改派必须备注；
3. 改衣赶不上开场硬冲突；同一候补相邻场次相交 → 演员场次重叠；
4. fit_rows 选定副本排程必须使用：自动匹配的固定副本等待复用（单副本超时），
   人工改派的固定副本被并发占用且无替代 → copy_pin 冲突且不放置交叠；
   开场前已穿着段（init）也固定到选定副本；
5. 确认版冻结卡司/适配决定；原角或候补尺寸变化都只标记相关分支待复核；
6. API 全链路：候补顺位/尺寸/副本适配登记 → 从修订开分支 → 确认 → 导出。

运行：python3 test_understudy.py
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import db
import understudy

PASS = []


def check(name, fn):
    fn()
    PASS.append(name)
    print(f"  ok - {name}")


# ---------------- 直接引擎用例（state dict，副本 id 显式给出） ----------------

def item_row(iid=1, copies=2, closure="zip", don=10, doff=8, kind="costume"):
    return {"id": iid, "production_id": 1,
            "name": "长袍" if kind == "costume" else "皮靴",
            "kind": kind, "layer": 2, "don_sec": don, "doff_sec": doff,
            "status": "ok", "available_at": 0, "cart_id": None,
            "copies": copies, "skill_id": None, "closure": closure}


def task_row(tid, actor_id, fs=1, ts=2, dresser_id=1, start=None, locked=0):
    return {"id": tid, "production_id": 1, "actor_id": actor_id,
            "from_scene_id": fs, "to_scene_id": ts, "exit_side": "L",
            "position_id": None, "dresser_id": dresser_id, "start_sec": start,
            "locked": locked, "needs_review": 0, "note": ""}


def fit_row(copy_id, lo, hi, alt=0, asec=0, dim="chest"):
    return {"production_id": 1, "copy_id": copy_id, "dim": dim,
            "lo": lo, "hi": hi, "alterable": alt, "alter_sec": asec}


M1 = {"actor_id": 1, "height": 175, "chest": 95, "waist": 80,
      "hip": 95, "shoulder": 42, "foot": 26}
M2_SAME = {"actor_id": 2, "height": 175, "chest": 95, "waist": 80,
           "hip": 95, "shoulder": 42, "foot": 26}
M2_BIG = {"actor_id": 2, "height": 175, "chest": 100, "waist": 80,
          "hip": 95, "shoulder": 42, "foot": 26}


def make_state(measures=None, copies=2, fit_rows=None, item_closure="zip",
               tasks=None, looks=None, look_items=None, copy_closures=None):
    """候补本人无造型：只有原角（演员1）的 looks，验证换角后沿用原角色造型。"""
    scenes = [
        {"id": 1, "production_id": 1, "seq": 1, "name": "S1",
         "start_sec": 0, "duration_sec": 100},
        {"id": 2, "production_id": 1, "seq": 2, "name": "S2",
         "start_sec": 200, "duration_sec": 100},
        {"id": 3, "production_id": 1, "seq": 3, "name": "S3",
         "start_sec": 260, "duration_sec": 100},
    ]
    actors = [
        {"id": 1, "production_id": 1, "name": "甲", "code": "", "default_side": "L"},
        {"id": 2, "production_id": 1, "name": "乙", "code": "", "default_side": "L"},
    ]
    copy_rows = [{"id": 11 + i, "production_id": 1, "item_id": 1,
                  "copy_no": 1 + i, "label": "",
                  "closure": (copy_closures[i] if copy_closures else "")}
                 for i in range(copies)]
    if looks is None:
        # 原角 S1 不穿长袍、S2 穿上（换装任务才有 don 动作）；
        # 候补乙完全无个人造型，换角后克隆原角造型
        looks = [
            {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""},
        ]
    if look_items is None:
        look_items = [{"look_id": 2, "item_id": 1, "ord": 0}]
    return {
        "scenes": scenes, "actors": actors,
        "items": [item_row(1, copies=copies, closure=item_closure)],
        "looks": looks, "look_items": look_items,
        "dressers": [{"id": 1, "production_id": 1, "name": "王姐"}],
        "positions": [], "carts": [],
        "skills": [], "dresser_skills": [], "dresser_sides": [],
        "dresser_unavailable": [], "action_specs": [], "action_staff": [],
        "action_reviews": [],
        "tasks": tasks if tasks is not None else [task_row(1, 1)],
        "actor_measures": measures or [],
        "understudy_roster": [],
        "item_copies": copy_rows,
        "copy_fit": fit_rows or [],
    }


def test_inherit_original_look_and_penalties():
    # 乙无任何个人造型；换角后克隆原角 S1/S2 造型，2 副本 → 合身可确认
    st = make_state(measures=[M1, M2_BIG], copies=2)
    p = understudy.build_branch(st, {1: 2}, alter_start_sec=0)
    assert p["can_confirm"], f"应沿用原角造型并合身：{[c['message'] for c in p['conflicts']]}"
    assert {f["task_id"] for f in p["fit_rows"]} == {1}
    don = next(a for a in p["actions"] if a["task_id"] == 1 and a["kind"] == "don")
    assert don["dur"] == 15, f"胸围差 5cm 应 +5s，实得 {don['dur']}"
    # 候补自己的同名场次造型应被忽略：再给乙加一个空 S2 造型，结果不变
    st2 = make_state(measures=[M1, M2_BIG], copies=2)
    st2["looks"].append({"id": 9, "production_id": 1, "actor_id": 2,
                         "scene_id": 2, "name": "乙个人造型"})
    p2 = understudy.build_branch(st2, {1: 2})
    assert p2["can_confirm"] and len(p2["fit_rows"]) == len(p["fit_rows"])


def test_closure_penalties():
    st = make_state(measures=[M1, M2_SAME], copies=2, copy_closures=["", "tie"])
    # 自动：无适配区间，两件都合身；人工钉系带副本（12）→ 穿 +10
    p = understudy.build_branch(st, {1: 2}, assigns={"1:1": 12},
                                notes={"1:1": "用系带件"})
    don = next(a for a in p["actions"] if a["task_id"] == 1 and a["kind"] == "don")
    assert don["dur"] == 20, f"系带穿应+10，实得 {don['dur']}"
    # 脱下加时：乙在 S2 穿、S3 不穿的替演任务
    st3 = make_state(
        measures=[M1, M2_SAME], copies=2, copy_closures=["", "tie"],
        looks=[{"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
               {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""}],
        look_items=[{"look_id": 1, "item_id": 1, "ord": 0},
                    {"look_id": 2, "item_id": 1, "ord": 0}],
        tasks=[task_row(1, 1, 1, 2), task_row(2, 2, 2, 3)])
    # 脱下加时：乙开场前已穿、S3 脱下离场；人工改派系带副本（12）→ 脱 +8
    p4 = understudy.build_branch(st3, {1: 2, 2: 2},
                                 assigns={"2:1": 12},
                                 notes={"2:1": "系带脱下"})
    doff4 = next(a for a in p4["actions"] if a["task_id"] == 2 and a["kind"] == "doff")
    assert doff4["dur"] == 16, f"系带脱应+8，实得 {doff4['dur']}"
    # 脱下件必须落在人工改派的系带副本（fit_rows 与排程一致）
    assert p4["fit_rows"][0]["copy_no"] == 2
    assert any(c["copy_no"] == 2 and c["pre_show"] for c in p4["copies"])


def test_missing_and_out_of_range():
    # 乙无尺寸 + 副本有胸围区间 → 尺寸缺失（2 副本隔离复用）
    st = make_state(measures=[M1], copies=2,
                    fit_rows=[fit_row(11, 90, 100), fit_row(12, 90, 100)])
    p = understudy.build_branch(st, {1: 2})
    assert not p["can_confirm"]
    assert p["earliest"]["type"] == "measure", f"最早应是尺寸缺失：{p['earliest']}"
    # 乙 110 超出 90-98 不可调 → 越界
    st2 = make_state(measures=[M1, {**M2_BIG, "chest": 110}], copies=2,
                     fit_rows=[fit_row(11, 90, 98), fit_row(12, 90, 98)])
    p2 = understudy.build_branch(st2, {1: 2})
    assert any(c["type"] == "fit" for c in p2["conflicts"])


def test_boundary_and_manual_note():
    st = make_state(measures=[M1, M2_BIG], copies=2,
                    fit_rows=[fit_row(11, 90, 100), fit_row(12, 90, 100)])
    p = understudy.build_branch(st, {1: 2})
    assert not p["can_confirm"]
    assert any(c["type"] == "need_note" for c in p["conflicts"])
    p2 = understudy.build_branch(st, {1: 2}, notes={"1:1": "端点实测可穿，开场前复查"})
    assert p2["can_confirm"], f"备注后应可确认：{[c['message'] for c in p2['conflicts']]}"
    # 人工改派无备注 → 禁止确认
    st3 = make_state(measures=[M1, M2_SAME], copies=2)
    p3 = understudy.build_branch(st3, {1: 2}, assigns={"1:1": 12})
    assert not p3["can_confirm"]
    p4 = understudy.build_branch(st3, {1: 2}, assigns={"1:1": 12},
                                 notes={"1:1": "1号件留给原角"})
    assert p4["can_confirm"]


def test_alter_too_late():
    # 真实换装：原角 S1 无长袍、S2 穿上；2 副本让候补 101 能穿上副本1。
    # 副本1 区间 90-98（候补100越界、可调90s），副本2 区间 90-110 合身。
    # 人工钉副本1：改衣 180 才开工 → 穿上 101 前来不及；0 开工则赶得上。
    st = make_state(measures=[M1, M2_BIG], copies=2,
                    fit_rows=[fit_row(11, 90, 98, alt=1, asec=90),
                              fit_row(12, 90, 110)])
    p = understudy.build_branch(st, {1: 2}, assigns={"1:1": 11},
                                notes={"1:1": "安排改衣"}, alter_start_sec=180)
    dec = next(d for d in p["decisions"] if d["kind"] == "alter")
    assert dec["first_don"] > 0, f"首次穿上应在换装窗口内，实得 {dec['first_don']}"
    assert any(c["type"] == "alter_late" for c in p["conflicts"]), \
        f"应触发改衣超时：{[c['message'] for c in p['conflicts']]}"
    p2 = understudy.build_branch(st, {1: 2}, assigns={"1:1": 11},
                                 notes={"1:1": "安排改衣"}, alter_start_sec=0)
    assert not any(c["type"] == "alter_late" for c in p2["conflicts"])


def test_cast_overlap():
    looks = [
        {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
        {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""},
        {"id": 3, "production_id": 1, "actor_id": 1, "scene_id": 3, "name": ""},
    ]
    li = [{"look_id": 1, "item_id": 1, "ord": 0},
          {"look_id": 2, "item_id": 1, "ord": 0},
          {"look_id": 3, "item_id": 1, "ord": 0}]
    tasks = [task_row(1, 1, 1, 2), task_row(2, 2, 2, 3)]
    st = make_state(measures=[M1, M2_SAME], copies=3, looks=looks,
                    look_items=li, tasks=tasks)
    p = understudy.build_branch(st, {1: 2, 2: 2})
    assert any(c["type"] == "cast_overlap" for c in p["conflicts"])
    # 同场连戏/换装窗口不误报
    p2 = understudy.build_branch(make_state(measures=[M1, M2_SAME], copies=2), {1: 2})
    assert not any(c["type"] == "cast_overlap" for c in p2["conflicts"])


def test_fixed_copy_wait_and_pin_busy():
    # 人工钉唯一件：候补 don 与原角未替任务的穿着段冲突 → 等待复用超时/改派冲突。
    # 原角任务2（未拖换）在 S3 仍穿长袍，候补任务1 人工钉唯一件
    looks = [
        {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
        {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""},
        {"id": 3, "production_id": 1, "actor_id": 1, "scene_id": 3, "name": ""},
    ]
    li = [{"look_id": 1, "item_id": 1, "ord": 0},
          {"look_id": 2, "item_id": 1, "ord": 0},
          {"look_id": 3, "item_id": 1, "ord": 0}]
    tasks = [task_row(1, 1, 1, 2), task_row(2, 1, 2, 3)]
    st = make_state(measures=[M1, M2_SAME], copies=1, looks=looks,
                    look_items=li, tasks=tasks)
    p = understudy.build_branch(st, {1: 2}, assigns={"1:1": 11},
                                notes={"1:1": "固定唯一件"})
    assert not p["can_confirm"]
    # 等待复用导致超时，或改派副本被并发占用
    types = {c["type"] for c in p["conflicts"]}
    assert "late" in types or "copy_pin" in types, \
        f"固定副本冲突应报超时或改派占用：{types}"
    assert {c["copy_no"] for c in p["copies"]} <= {1}, "不得换用其它副本"


def test_init_segment_pinned():
    """候补只替「脱下」任务（原角开场前已穿该件）：fit_rows 与排程都固定到选定副本。"""
    looks = [
        {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
        {"id": 3, "production_id": 1, "actor_id": 1, "scene_id": 3, "name": ""},
    ]
    li = [{"look_id": 1, "item_id": 1, "ord": 0},
          {"look_id": 3, "item_id": 1, "ord": 0}]
    # 任务：演员1（原角）S1→S3 脱下长袍（S2 不穿）；候补乙替该任务
    tasks = [task_row(2, 1, 1, 3)]
    st = make_state(measures=[M1, M2_SAME], copies=2, looks=looks,
                    look_items=li, tasks=tasks)
    p = understudy.build_branch(st, {2: 2}, notes={"2:1": "开场前已穿，沿用1号件"})
    assert p["can_confirm"], f"仅脱下替演应可确认：{[c['message'] for c in p['conflicts']]}"
    init_rows = [f for f in p["fit_rows"] if f["pre_show"]]
    assert init_rows and init_rows[0]["copy_no"] in (1, 2)
    # 排程日历中开场穿着段落在选定副本上
    pre = [c for c in p["copies"] if c["pre_show"]]
    assert pre, "开场前穿着段落应进入副本日历"


# ---------------- API 全链路 ----------------

def _seed_basic_db(con, copies=2):
    con.execute("INSERT INTO scenes(production_id,seq,name,start_sec,duration_sec)"
                " VALUES(1,1,'S1',0,100)")
    con.execute("INSERT INTO scenes(production_id,seq,name,start_sec,duration_sec)"
                " VALUES(1,2,'S2',200,100)")
    con.execute("INSERT INTO actors(production_id,name) VALUES(1,'甲')")
    con.execute("INSERT INTO actors(production_id,name) VALUES(1,'乙')")
    con.execute("INSERT INTO dressers(production_id,name) VALUES(1,'王姐')")
    con.execute("INSERT INTO items(production_id,name,kind,layer,don_sec,doff_sec,status,"
                "available_at,cart_id,copies,closure) "
                "VALUES(1,'长袍','costume',2,10,8,'ok',0,NULL,?,'zip')", (copies,))
    con.execute("INSERT INTO looks(production_id,actor_id,scene_id,name) VALUES(1,1,1,'')")
    con.execute("INSERT INTO looks(production_id,actor_id,scene_id,name) VALUES(1,1,2,'')")
    # S1 无长袍、S2 穿上（换装任务才有 don）
    con.execute("INSERT INTO look_items(look_id,item_id,ord) VALUES(2,1,0)")
    con.execute("INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,"
                "exit_side,position_id,dresser_id,start_sec,locked) "
                "VALUES(1,1,1,2,'L',NULL,1,NULL,0)")
    con.commit()


def test_api_flow_freeze_review_export():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.DB_PATH = tmp
    db.init_db()
    import app as web
    client = web.app.test_client()
    con = db.connect()
    _seed_basic_db(con)
    # 同步实物副本（id 从 1 自增）
    db.sync_item_copies(1)
    copies = {r["copy_no"]: r["id"] for r in con.execute(
        "SELECT id,copy_no FROM item_copies WHERE item_id=1").fetchall()}
    con.close()

    assert client.post("/api/understudy/roster",
                       json={"role_actor_id": 1, "under_actor_id": 2,
                             "priority": 1}).status_code == 200
    assert client.post("/api/actors/2/measures",
                       json={"height": 175, "chest": 95, "waist": 80, "hip": 95,
                             "shoulder": 42, "foot": 26}).status_code == 200
    assert client.post(f"/api/item_copies/{copies[1]}/fit",
                       json={"dim": "chest", "lo": 90, "hi": 100}).status_code == 200
    r = client.post("/api/revisions", json={"note": "午场前"})
    rev_id = r.get_json()["revisions"][0]["id"]

    r = client.post("/api/understudy/branches",
                    json={"revision_id": rev_id, "name": "乙替甲", "cast": {"1": 2}})
    assert r.status_code == 200, r.data
    bid = r.get_json()["id"]
    detail = r.get_json()["detail"]
    assert detail["plan"]["can_confirm"], \
        f"同尺寸沿用原角造型应可确认：{[c['message'] for c in detail['plan']['conflicts']]}"
    # fit_rows 与排程实际占用一致
    fr = detail["plan"]["fit_rows"][0]
    used = {c["copy_no"] for c in detail["plan"]["copies"] if c["task_id"] == 1}
    assert fr["copy_no"] in used, "冻结计划必须使用 fit_rows 选定的副本"

    assert client.post(f"/api/understudy/branches/{bid}/confirm").status_code == 200
    # 冻结：拖换/改派/删除全部 409
    assert client.post(f"/api/understudy/branches/{bid}/cast",
                       json={"task_id": 1, "actor_id": 1}).status_code == 409
    assert client.post(f"/api/understudy/branches/{bid}/assign",
                       json={"task_id": 1, "item_id": 1, "copy_id": copies[2],
                             "note": "x"}).status_code == 409
    assert client.delete(f"/api/understudy/branches/{bid}").status_code == 409

    # 候补尺寸变化 → 相关确认分支标待复核，冻结计划不变
    frozen_before = json.loads(db.get_branch(bid)["plan_json"])
    client.post("/api/actors/2/measures", json={"chest": 99})
    assert db.get_branch(bid)["needs_review"] == 1
    assert json.loads(db.get_branch(bid)["plan_json"]) == frozen_before
    client.post(f"/api/understudy/branches/{bid}/clear_review")
    # 原角尺寸变化（影响偏差加时）也必须标记
    client.post("/api/actors/1/measures",
                json={"height": 175, "chest": 90, "waist": 80, "hip": 95,
                      "shoulder": 42, "foot": 26})
    assert db.get_branch(bid)["needs_review"] == 1, "原角尺寸变化应标记相关分支"
    # 无关演员不标记
    client.post(f"/api/understudy/branches/{bid}/clear_review")
    con = db.connect()
    con.execute("INSERT INTO actors(production_id,name) VALUES(1,'路人丙')")
    con.commit(); con.close()
    client.post("/api/actors/3/measures", json={"chest": 120})
    assert db.get_branch(bid)["needs_review"] == 0, "无关演员变化不应标记"

    r = client.get(f"/export/understudy/{bid}/sheet")
    assert r.status_code == 200 and "乙".encode() in r.data
    r = client.get(f"/export/understudy/{bid}/diff.svg")
    assert r.status_code == 200 and r.data.startswith(b"<svg")
    os.unlink(tmp)


def test_confirm_blocked_with_conflict():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.DB_PATH = tmp
    db.init_db()
    import app as web
    client = web.app.test_client()
    con = db.connect()
    _seed_basic_db(con, copies=2)
    db.sync_item_copies(1)
    copies = {r["copy_no"]: r["id"] for r in con.execute(
        "SELECT id,copy_no FROM item_copies WHERE item_id=1").fetchall()}
    con.close()
    client.post("/api/actors/2/measures",
                json={"height": 175, "chest": 110, "waist": 100,
                      "hip": 110, "shoulder": 44, "foot": 27})
    client.post(f"/api/item_copies/{copies[1]}/fit",
                json={"dim": "chest", "lo": 90, "hi": 100})
    client.post(f"/api/item_copies/{copies[2]}/fit",
                json={"dim": "chest", "lo": 90, "hi": 100})
    rev_id = client.post("/api/revisions", json={"note": "base"}).get_json()[
        "revisions"][0]["id"]
    bid = client.post("/api/understudy/branches",
                      json={"revision_id": rev_id, "cast": {"1": 2}}).get_json()["id"]
    r = client.post(f"/api/understudy/branches/{bid}/confirm")
    assert r.status_code == 409, f"有越界冲突时确认应 409，实得 {r.status_code}"
    os.unlink(tmp)


def test_drag_cast_and_assign_endpoints():
    """从修订开分支后拖换/还原卡司、改派需备注、重算清待复核。"""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.DB_PATH = tmp
    db.init_db()
    import app as web
    client = web.app.test_client()
    con = db.connect()
    _seed_basic_db(con)
    db.sync_item_copies(1)
    copies = {r["copy_no"]: r["id"] for r in con.execute(
        "SELECT id,copy_no FROM item_copies WHERE item_id=1").fetchall()}
    con.close()
    client.post("/api/actors/1/measures",
                json={"height": 175, "chest": 95, "waist": 80, "hip": 95,
                      "shoulder": 42, "foot": 26})
    client.post("/api/actors/2/measures",
                json={"height": 175, "chest": 95, "waist": 80, "hip": 95,
                      "shoulder": 42, "foot": 26})
    rev_id = client.post("/api/revisions", json={"note": "r"}).get_json()[
        "revisions"][0]["id"]
    bid = client.post("/api/understudy/branches",
                      json={"revision_id": rev_id, "cast": {}}).get_json()["id"]
    r = client.post(f"/api/understudy/branches/{bid}/cast",
                    json={"task_id": 1, "actor_id": 2})
    assert r.status_code == 200 and r.get_json()["detail"]["cast"]["1"] == 2
    # 无备注册改派 → 400
    r = client.post(f"/api/understudy/branches/{bid}/assign",
                    json={"task_id": 1, "item_id": 1, "copy_id": copies[2], "note": ""})
    assert r.status_code == 400
    r = client.post(f"/api/understudy/branches/{bid}/assign",
                    json={"task_id": 1, "item_id": 1, "copy_id": copies[2],
                          "note": "1号件留原角"})
    assert r.status_code == 200
    assert r.get_json()["detail"]["assigns"]["1:1"] == copies[2]
    # 还原原角 → 卡司清空
    r = client.post(f"/api/understudy/branches/{bid}/cast",
                    json={"task_id": 1, "actor_id": 0})
    assert r.status_code == 200 and "1" not in r.get_json()["detail"]["cast"]
    os.unlink(tmp)


def test_branch_based_on_revision_ignores_new_tasks():
    """旧修订之后当前方案新增的任务不得混入分支：
    - build_branch 忽略 cast 中快照外的任务（ignored_cast），不抛 KeyError；
    - /cast 接口对快照外任务返回 400，不 500；
    - 计划含独立 orig_windows/base_tasks/base_scenes，差异 SVG 用两套窗口。"""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.DB_PATH = tmp
    db.init_db()
    import app as web
    client = web.app.test_client()
    con = db.connect()
    _seed_basic_db(con)
    db.sync_item_copies(1)
    con.close()
    client.post("/api/actors/2/measures",
                json={"height": 175, "chest": 95, "waist": 80, "hip": 95,
                      "shoulder": 42, "foot": 26})
    rev_id = client.post("/api/revisions", json={"note": "旧修订"}).get_json()[
        "revisions"][0]["id"]
    # 修订之后新增任务 99（当前方案任务集）
    r = client.post("/api/tasks", json={
        "actor_id": 1, "from_scene_id": 1, "to_scene_id": 2,
        "exit_side": "L", "position_id": None, "dresser_id": 1})
    assert r.status_code == 200

    # 1) 直接引擎：state 含新任务，快照只有任务1；cast 引用 99 被忽略
    rev = db.get_revision(rev_id)
    cur = db.load_state(1)
    base = understudy.base_state_for(cur, rev)
    assert {t["id"] for t in base["tasks"]} == {1}
    p = understudy.build_branch(base, {1: 2, 99: 2})
    assert 99 in p["ignored_cast"], f"新任务应被忽略：{p['ignored_cast']}"
    assert "99" not in p["cast"] and 99 not in p["swapped"]
    # 两套窗口与基准快照
    assert "1" in p["orig_windows"] and "1" in p["windows"]
    assert {t["id"] for t in p["base_tasks"]} == {1}
    assert {s["id"] for s in p["base_scenes"]} == {1, 2}

    # 2) 创建分支引用新任务不 500（忽略）
    r = client.post("/api/understudy/branches",
                    json={"revision_id": rev_id, "name": "b", "cast": {"1": 2, "99": 2}})
    assert r.status_code == 200, r.data
    bid = r.get_json()["id"]
    assert 99 in r.get_json()["detail"]["plan"]["ignored_cast"]

    # 3) /cast 对快照外任务返回 400
    r = client.post(f"/api/understudy/branches/{bid}/cast",
                    json={"task_id": 99, "actor_id": 2})
    assert r.status_code == 400, f"快照外任务换角应 400，实得 {r.status_code}"
    # 快照内任务正常
    r = client.post(f"/api/understudy/branches/{bid}/cast",
                    json={"task_id": 1, "actor_id": 2})
    assert r.status_code == 200

    # 4) 差异 SVG 使用分支冻结的两套窗口：上排原计划/下排替演都出现
    svg = client.get(f"/export/understudy/{bid}/diff.svg").data.decode()
    assert "#1原" in svg and "#1替" in svg, "差异 SVG 应叠放原计划与替演两层"
    # 新任务不得出现在旧修订分支的 SVG
    assert "#99" not in svg
    os.unlink(tmp)


def test_orig_windows_reflect_plan_before_swap():
    """orig_windows 是未换角的原计划：换角导致替演窗口变化时两者起止不同。"""
    # 候补尺寸更大导致穿上更慢（偏差加时），替演窗口应晚于原计划
    st = make_state(measures=[M1, {**M2_BIG, "chest": 107, "waist": 90,
                                   "hip": 107, "shoulder": 46}], copies=2)
    p = understudy.build_branch(st, {1: 2}, notes={"1:1": "候补偏大，试穿通过"})
    w0, w1 = p["orig_windows"]["1"], p["windows"]["1"]
    don_orig = next(a for a in p["orig_actions"] if a["kind"] == "don")
    don_new = next(a for a in p["actions"]
                   if a["task_id"] == 1 and a["kind"] == "don")
    assert don_new["dur"] > don_orig["dur"], "换角后穿脱时长应反映尺寸偏差"
    assert w1["start"] >= w0["start"]
    # 原计划不随 cast 变化：同基准、不同 cast 的 orig_windows 一致
    p2 = understudy.build_branch(st, {})
    assert p2["orig_windows"]["1"] == p["orig_windows"]["1"]


if __name__ == "__main__":
    print("替演推演测试：")
    check("候补沿用原角造型（个人造型忽略）+偏差加时", test_inherit_original_look_and_penalties)
    check("闭合件调整穿/脱时长", test_closure_penalties)
    check("尺寸缺失/适配越界硬冲突并定位最早", test_missing_and_out_of_range)
    check("边界尺寸/人工改派必须备注", test_boundary_and_manual_note)
    check("改衣赶不上开场硬冲突", test_alter_too_late)
    check("候补相邻场次重叠；同场连戏不误报", test_cast_overlap)
    check("固定副本等待复用/并发占用报 copy_pin 且无交叠", test_fixed_copy_wait_and_pin_busy)
    check("开场前已穿着段固定到选定副本（仅脱下替演）", test_init_segment_pinned)
    check("API：登记→开分支→确认冻结→尺寸变化待复核→导出", test_api_flow_freeze_review_export)
    check("有硬冲突时禁止确认（409）", test_confirm_blocked_with_conflict)
    check("拖换卡司/还原/改派备注接口", test_drag_cast_and_assign_endpoints)
    check("旧修订后新增任务不混入分支；两套窗口；差异SVG", test_branch_based_on_revision_ignores_new_tasks)
    check("orig_windows 为换角前原计划且不随卡司变化", test_orig_windows_reflect_plan_before_swap)
    print(f"全部通过（{len(PASS)} 项）")
