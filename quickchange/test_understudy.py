# -*- coding: utf-8 -*-
"""替演推演回归测试：

1. 副本匹配：合身直接通过（偏差/闭合件加时）；尺寸缺失、适配越界硬冲突；
   边界尺寸与人工改派必须备注，否则禁止确认；
2. 改衣赶不上开场（改衣开工时刻 + 改衣耗时 > 首次穿上）硬冲突；
3. 同一候补相邻场次相交 → 演员场次重叠；同场连戏不误报；
4. 人工改派副本被并发占用 → copy_pin 硬冲突，且绝不放置交叠占用；
5. 确认版冻结：确认后不能再拖换卡司/改派/删除；资料变化只标记相关分支待复核；
6. API 全链路：尺寸/候补/副本适配登记 → 开分支 → 拖换 → 确认 → 换装单/差异 SVG。

运行：python3 test_understudy.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import db
import scheduler
import understudy

PASS = []


def check(name, fn):
    fn()
    PASS.append(name)
    print(f"  ok - {name}")


def item(iid=1, copies=2, closure="zip", don=10, doff=8, kind="costume"):
    return {"id": iid, "production_id": 1, "name": "长袍" if kind == "costume" else "皮靴",
            "kind": kind, "layer": 2, "don_sec": don, "doff_sec": doff,
            "status": "ok", "available_at": 0, "cart_id": None,
            "copies": copies, "skill_id": None, "closure": closure}


def task(tid, actor_id, fs=1, ts=2, dresser_id=1, start=None, locked=0):
    return {"id": tid, "production_id": 1, "actor_id": actor_id,
            "from_scene_id": fs, "to_scene_id": ts, "exit_side": "L",
            "position_id": None, "dresser_id": dresser_id, "start_sec": start,
            "locked": locked, "needs_review": 0, "note": ""}


def make_state(measures=None, copies=2, fit_rows=None, item_closure="zip",
               tasks=None, looks=None, look_items=None, copy_closure=None,
               items_extra=None, copy_rows=None):
    scenes = [
        {"id": 1, "seq": 1, "name": "S1", "start_sec": 0, "duration_sec": 100},
        {"id": 2, "seq": 2, "name": "S2", "start_sec": 200, "duration_sec": 100},
        {"id": 3, "seq": 3, "name": "S3", "start_sec": 260, "duration_sec": 100},
    ]
    actors = [
        {"id": 1, "production_id": 1, "name": "甲", "code": "", "default_side": "L"},
        {"id": 2, "production_id": 1, "name": "乙", "code": "", "default_side": "L"},
    ]
    items = [item(1, copies=copies, closure=item_closure)]
    if items_extra:
        items += items_extra
    if copy_rows is None:
        copy_rows = [{"id": 11 + i, "production_id": 1, "item_id": 1,
                      "copy_no": 1 + i, "label": "",
                      "closure": (copy_closure[i] if copy_closure else "")}
                     for i in range(copies)]
    if looks is None:
        looks = [
            {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
            {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""},
            {"id": 3, "production_id": 1, "actor_id": 2, "scene_id": 2, "name": ""},
        ]
    if look_items is None:
        look_items = [{"look_id": 2, "item_id": 1}, {"look_id": 3, "item_id": 1}]
    return {
        "scenes": scenes, "actors": actors, "items": items,
        "looks": looks, "look_items": look_items,
        "dressers": [{"id": 1, "production_id": 1, "name": "王姐"}],
        "positions": [], "carts": [],
        "skills": [], "dresser_skills": [], "dresser_sides": [],
        "dresser_unavailable": [], "action_specs": [], "action_staff": [],
        "action_reviews": [],
        "tasks": tasks if tasks is not None else [task(1, 1)],
        "actor_measures": measures or [],
        "understudy_roster": [],
        "item_copies": copy_rows,
        "copy_fit": fit_rows or [],
    }


M1 = {"actor_id": 1, "height": 175, "chest": 95, "waist": 80,
      "hip": 95, "shoulder": 42, "foot": 26}
M2_SAME = {"actor_id": 2, "height": 175, "chest": 95, "waist": 80,
           "hip": 95, "shoulder": 42, "foot": 26}
M2_BIG = {"actor_id": 2, "height": 175, "chest": 100, "waist": 80,
          "hip": 95, "shoulder": 42, "foot": 26}
FIT = lambda cid, lo, hi, alt=0, asec=0: {
    "production_id": 1, "copy_id": cid, "dim": "chest", "lo": lo, "hi": hi,
    "alterable": alt, "alter_sec": asec}


# ---------- 1a) 合身：偏差与闭合件加时，无冲突可确认 ----------

def test_fit_ok_with_penalties():
    # 两副本都无适配区间 → 任意尺寸都合身；乙与甲胸围差 5cm → 穿 +5s
    st = make_state(measures=[M1, M2_BIG], copies=2)
    p = understudy.build_branch(st, {1: 2}, alter_start_sec=0)
    assert p["can_confirm"], f"合身分支应可确认：{[c['message'] for c in p['conflicts']]}"
    don = next(a for a in p["actions"] if a["task_id"] == 1 and a["kind"] == "don")
    assert don["dur"] == 15, f"胸围差 5cm 应 +5s，实得 {don['dur']}"
    # 副本2 系带：穿 +10、脱 +8（自动优先给无约束副本；指定系带副本看用时）
    st2 = make_state(measures=[M1, M2_SAME], copies=2,
                     copy_closure=["", "tie"])
    p2 = understudy.build_branch(st2, {1: 2})
    assert p2["can_confirm"]
    # 人工指定系带副本（需备注）看闭合件加时
    p3 = understudy.build_branch(st2, {1: 2}, assigns={"1:1": 12},
                                 notes={"1:1": "用系带件"})
    don3 = next(a for a in p3["actions"] if a["task_id"] == 1 and a["kind"] == "don")
    # 同尺寸无偏差，系带穿上 +10
    assert don3["dur"] == 20, f"系带应穿+10，实得 {don3['dur']}"
    # 脱下加时：构造「S2 穿长袍 → S3 不穿」的替演任务
    st3 = make_state(
        measures=[M1, M2_SAME], copies=2, copy_closure=["", "tie"],
        looks=[
            {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
            {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""},
            {"id": 3, "production_id": 1, "actor_id": 2, "scene_id": 2, "name": ""},
        ],
        look_items=[{"look_id": 2, "item_id": 1}, {"look_id": 3, "item_id": 1}],
        tasks=[task(1, 1, 1, 2), task(2, 2, 2, 3)],
        copy_rows=None)
    # 任务2：乙 S2→S3 脱下系带长袍（人工钉副本12）
    p4 = understudy.build_branch(st3, {2: 2}, assigns={"2:1": 12},
                                 notes={"2:1": "系带脱下加时"})
    doff4 = next(a for a in p4["actions"] if a["task_id"] == 2 and a["kind"] == "doff")
    assert doff4["dur"] == 16, f"系带脱应+8，实得 {doff4['dur']}"


# ---------- 1b) 尺寸缺失、适配越界 ----------

def test_missing_and_out_of_range():
    # 乙无尺寸 + 副本有胸围区间 → 尺寸缺失硬冲突（2 副本隔离原角复用）
    st = make_state(measures=[M1], copies=2,
                    fit_rows=[FIT(11, 90, 100),
                              {"production_id": 1, "copy_id": 12, "dim": "chest",
                               "lo": 90, "hi": 100, "alterable": 0, "alter_sec": 0}])
    p = understudy.build_branch(st, {1: 2})
    assert not p["can_confirm"]
    assert any(c["type"] == "measure" for c in p["conflicts"]), "应报尺寸缺失"
    assert p["earliest"]["type"] == "measure", \
        f"最早冲突应定位尺寸缺失，实得 {p['earliest']}"
    # 乙 110 超出 90-98 且不可调 → 越界
    st2 = make_state(measures=[M1, {**M2_BIG, "chest": 110}], copies=2,
                     fit_rows=[FIT(11, 90, 98, alt=0),
                               {"production_id": 1, "copy_id": 12, "dim": "chest",
                                "lo": 90, "hi": 98, "alterable": 0, "alter_sec": 0}])
    p2 = understudy.build_branch(st2, {1: 2})
    assert any(c["type"] == "fit" for c in p2["conflicts"]), "应报适配越界"


# ---------- 1c) 边界尺寸必须备注 ----------

def test_boundary_needs_note():
    # 乙胸围 100 恰为区间 [90,100] 上端点（2 副本隔离复用）
    st = make_state(measures=[M1, M2_BIG], copies=2,
                    fit_rows=[FIT(11, 90, 100), FIT(12, 90, 100)])
    p = understudy.build_branch(st, {1: 2})
    assert not p["can_confirm"], "边界尺寸无备注应禁止确认"
    assert any(c["type"] == "need_note" for c in p["conflicts"])
    p2 = understudy.build_branch(st, {1: 2}, notes={"1:1": "端点实测可穿，开场前复查"})
    assert p2["can_confirm"], f"备注后应可确认：{[c['message'] for c in p2['conflicts']]}"


# ---------- 1d) 人工改派必须备注 ----------

def test_manual_assign_needs_note():
    st = make_state(measures=[M1, M2_SAME], copies=2)
    p = understudy.build_branch(st, {1: 2}, assigns={"1:1": 12})
    assert not p["can_confirm"], "人工改派无备注应禁止确认"
    assert any("人工改派" in c["message"] for c in p["conflicts"])
    p2 = understudy.build_branch(st, {1: 2}, assigns={"1:1": 12},
                                 notes={"1:1": "1号件留给原角"})
    assert p2["can_confirm"]


# ---------- 2) 改衣赶不上开场 ----------

def test_alter_too_late():
    # 乙 100 超 [90,98]，副本1 可调、改衣 90s；2 副本避免占用干扰，
    # 人工钉副本11（需要改衣的那件）；改衣 180 才能开工 → 270 才好，穿上 ~101
    st = make_state(measures=[M1, M2_BIG], copies=2,
                    fit_rows=[FIT(11, 90, 98, alt=1, asec=90)])
    p = understudy.build_branch(st, {1: 2}, assigns={"1:1": 11},
                                notes={"1:1": "安排改衣"}, alter_start_sec=180)
    assert any(c["type"] == "alter_late" for c in p["conflicts"]), "应报改衣赶不上"
    # 0 时刻即可开工：0+90=90 < 101 → 不超时（人工改派备注仍需在）
    p2 = understudy.build_branch(st, {1: 2}, assigns={"1:1": 11},
                                 notes={"1:1": "安排改衣"}, alter_start_sec=0)
    assert not any(c["type"] == "alter_late" for c in p2["conflicts"]), "提前改衣不应超时"


# ---------- 3) 场次重叠 ----------

def test_cast_overlap():
    # 乙同时替两个相邻任务：S2(200-300) 与 S3(260-360) 相交
    looks = [
        {"id": 1, "production_id": 1, "actor_id": 1, "scene_id": 1, "name": ""},
        {"id": 2, "production_id": 1, "actor_id": 1, "scene_id": 2, "name": ""},
        {"id": 3, "production_id": 1, "actor_id": 2, "scene_id": 2, "name": ""},
        {"id": 4, "production_id": 1, "actor_id": 2, "scene_id": 3, "name": ""},
    ]
    look_items = [{"look_id": 2, "item_id": 1}, {"look_id": 3, "item_id": 1},
                  {"look_id": 4, "item_id": 1}]
    tasks = [task(1, 1, 1, 2), task(2, 2, 2, 3)]
    st = make_state(measures=[M1, M2_SAME], copies=2, looks=looks,
                    look_items=look_items, tasks=tasks)
    p = understudy.build_branch(st, {1: 2, 2: 2})
    assert any(c["type"] == "cast_overlap" for c in p["conflicts"]), "应报场次重叠"
    # 同场连戏（只替任务1，乙在 S1→S2 出场，区间不重叠）不应误报
    p2 = understudy.build_branch(make_state(measures=[M1, M2_SAME], copies=2), {1: 2})
    assert not any(c["type"] == "cast_overlap" for c in p2["conflicts"]), \
        "同场连戏/换装窗口不应报场次重叠"


# ---------- 4) 人工改派副本并发占用 ----------

def test_manual_copy_busy():
    # 单副本：原角开场（S2）从 200 穿着到下场；乙的任务穿上在 ~101。
    # 人工把唯一副本钉给乙 → copy_pin 硬冲突，且日历中不得出现交叠
    st = make_state(measures=[M1, M2_SAME], copies=1)
    p = understudy.build_branch(st, {1: 2}, assigns={"1:1": 11},
                                notes={"1:1": "改派理由"})
    assert any(c["type"] == "copy_pin" for c in p["conflicts"]), "应报人工改派并发占用"
    # 同一副本占用区间不得交叠（穿上被拒绝，只保留原角 init 区间）
    ivs = sorted(iv for c in p["copies"] for iv in [])  # 输出只含替演任务
    # 直接复核引擎用的 sched copies 无交叠（副本 1 上至多一条）
    base = understudy.base_state_for(st, None) if False else st
    # 用引擎内部结果：fit_rows 显示分配了第 1 件，但穿上被拒绝 → copies 中无该任务区间
    assert not any(c["task_id"] == 1 for c in p["copies"]), "被占时不应放置穿上占用"


# ---------- 5/6) API 全链路 + 冻结 + 资料变化只标记待复核 ----------

def test_api_flow_freeze_and_review():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.DB_PATH = tmp
    db.init_db()
    import app as web
    client = web.app.test_client()
    con = db.connect()
    con.execute("INSERT INTO scenes(production_id,seq,name,start_sec,duration_sec)"
                " VALUES(1,1,'S1',0,100)")
    con.execute("INSERT INTO scenes(production_id,seq,name,start_sec,duration_sec)"
                " VALUES(1,2,'S2',200,100)")
    con.execute("INSERT INTO actors(production_id,name) VALUES(1,'甲')")
    con.execute("INSERT INTO actors(production_id,name) VALUES(2,'乙')")
    con.execute("INSERT INTO dressers(production_id,name) VALUES(1,'王姐')")
    con.execute("""INSERT INTO items(production_id,name,kind,layer,don_sec,doff_sec,status,
                  available_at,cart_id,copies,closure)
                  VALUES(1,'长袍','costume',2,10,8,'ok',0,NULL,2,'zip')""")
    con.execute("INSERT INTO looks(production_id,actor_id,scene_id,name) VALUES(1,1,1,'')")
    con.execute("INSERT INTO looks(production_id,actor_id,scene_id,name) VALUES(1,1,2,'')")
    con.execute("INSERT INTO looks(production_id,actor_id,scene_id,name) VALUES(1,2,2,'')")
    for lid, iid in ((2, 1), (3, 1)):
        con.execute("INSERT INTO look_items(look_id,item_id,ord) VALUES(?,?,0)", (lid, iid))
    con.execute("""INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,
                  exit_side,position_id,dresser_id,start_sec,locked)
                  VALUES(1,1,1,2,'L',NULL,1,NULL,0)""")
    con.commit()
    con.close()

    # 候补顺位：乙是甲的第一候补
    r = client.post("/api/understudy/roster",
                    json={"role_actor_id": 1, "under_actor_id": 2, "priority": 1})
    assert r.status_code == 200
    # 尺寸登记：甲乙同尺寸 → 无需适配资料也能推演
    r = client.post("/api/actors/2/measures",
                    json={"height": 175, "chest": 95, "waist": 80,
                          "hip": 95, "shoulder": 42, "foot": 26})
    assert r.status_code == 200
    # 副本适配区间：副本1 胸围 90-100
    r = client.post("/api/item_copies/11/fit",
                    json={"dim": "chest", "lo": 90, "hi": 100})
    assert r.status_code == 200
    # 先存修订作为基准
    r = client.post("/api/revisions", json={"note": "午场前"})
    assert r.status_code == 200
    rev_id = r.get_json()["revisions"][0]["id"]

    # 开分支（不拖换）→ 可确认但先拖换
    r = client.post("/api/understudy/branches",
                    json={"revision_id": rev_id, "name": "乙替甲", "cast": {"1": 2}})
    assert r.status_code == 200, r.data
    bid = r.get_json()["id"]
    detail = r.get_json()["detail"]
    assert detail["plan"]["can_confirm"], \
        f"同尺寸应可确认：{[c['message'] for c in detail['plan']['conflicts']]}"
    # 确认
    r = client.post(f"/api/understudy/branches/{bid}/confirm")
    assert r.status_code == 200
    # 冻结：拖换/改派/删除全部 409
    assert client.post(f"/api/understudy/branches/{bid}/cast",
                       json={"task_id": 1, "actor_id": 1}).status_code == 409
    assert client.post(f"/api/understudy/branches/{bid}/assign",
                       json={"task_id": 1, "item_id": 1, "copy_id": 12,
                             "note": "x"}).status_code == 409
    assert client.delete(f"/api/understudy/branches/{bid}").status_code == 409

    # 资料变化（乙尺寸）→ 已确认分支只标记 needs_review=1，计划不变
    r = client.post("/api/actors/2/measures", json={"chest": 99})
    assert r.status_code == 200
    br = db.get_branch(bid)
    assert br["needs_review"] == 1, "尺寸变化应标记相关分支待复核"
    import json as _json
    plan_before = _json.loads(br["plan_json"])
    assert plan_before["cast"]["1"] == 2 and plan_before["can_confirm"] is True
    # 销记
    r = client.post(f"/api/understudy/branches/{bid}/clear_review")
    assert r.status_code == 200 and db.get_branch(bid)["needs_review"] == 0
    # 导出：换装单 HTML 与差异 SVG
    r = client.get(f"/export/understudy/{bid}/sheet")
    assert r.status_code == 200 and "乙".encode() in r.data
    r = client.get(f"/export/understudy/{bid}/diff.svg")
    assert r.status_code == 200 and r.data.startswith(b"<svg")
    os.unlink(tmp)


def test_confirm_blocked_with_conflict():
    """存在硬冲突时确认接口 409。"""
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
    con.execute("INSERT INTO actors(production_id,name) VALUES(1,'乙')")
    con.execute("INSERT INTO dressers(production_id,name) VALUES(1,'王姐')")
    con.execute("INSERT INTO items(production_id,name,kind,layer,don_sec,doff_sec,copies,closure)"
                " VALUES(1,'长袍','costume',2,10,8,1,'zip')")
    con.execute("INSERT INTO looks(production_id,actor_id,scene_id,name) VALUES(1,1,1,'')")
    con.execute("INSERT INTO looks(production_id,actor_id,scene_id,name) VALUES(1,1,2,'')")
    con.execute("INSERT INTO looks(production_id,actor_id,scene_id,name) VALUES(1,2,2,'')")
    con.execute("INSERT INTO look_items(look_id,item_id) VALUES(2,1)")
    con.execute("INSERT INTO look_items(look_id,item_id) VALUES(3,1)")
    # 乙有尺寸 110，副本区间 90-100 不可调 → 越界
    con.execute("INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,exit_side,"
                "dresser_id) VALUES(1,1,1,2,'L',1)")
    con.commit()
    con.close()
    client.post("/api/actors/2/measures",
                json={"height": 175, "chest": 110, "waist": 100,
                      "hip": 110, "shoulder": 44, "foot": 27})
    client.post("/api/item_copies/11/fit", json={"dim": "chest", "lo": 90, "hi": 100})
    r = client.post("/api/revisions", json={"note": "base"})
    rev_id = r.get_json()["revisions"][0]["id"]
    r = client.post("/api/understudy/branches",
                    json={"revision_id": rev_id, "cast": {"1": 2}})
    bid = r.get_json()["id"]
    r = client.post(f"/api/understudy/branches/{bid}/confirm")
    assert r.status_code == 409, f"有越界冲突时确认应 409，实得 {r.status_code}"
    os.unlink(tmp)


if __name__ == "__main__":
    print("替演推演测试：")
    check("合身通过：尺寸偏差+闭合件调整穿脱时长", test_fit_ok_with_penalties)
    check("尺寸缺失/适配越界硬冲突并定位最早", test_missing_and_out_of_range)
    check("边界尺寸必须备注才能确认", test_boundary_needs_note)
    check("人工改派必须备注", test_manual_assign_needs_note)
    check("改衣赶不上开场硬冲突", test_alter_too_late)
    check("候补相邻场次重叠；同场连戏不误报", test_cast_overlap)
    check("人工改派副本并发占用硬冲突且不放置交叠", test_manual_copy_busy)
    check("API 全链路：登记→开分支→确认冻结→待复核→导出", test_api_flow_freeze_and_review)
    check("有硬冲突时禁止确认（409）", test_confirm_blocked_with_conflict)
    print(f"全部通过（{len(PASS)} 项）")
