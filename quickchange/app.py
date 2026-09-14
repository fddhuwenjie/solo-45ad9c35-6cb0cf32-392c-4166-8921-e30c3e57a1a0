"""Flask 入口：规则计算、排程 API、修订与导出。"""
import csv
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".pylibs"))

from flask import Flask, Response, jsonify, render_template, request

import db
import rehearsal
import scheduler
import turnaround
import understudy

app = Flask(__name__)
PID = 1

# 允许录入/修改的字段白名单
FIELDS = {
    "scenes": ["seq", "name", "start_sec", "duration_sec"],
    "actors": ["name", "code", "default_side"],
    "looks": ["actor_id", "scene_id", "name"],
    "items": ["name", "kind", "layer", "don_sec", "doff_sec", "status",
              "available_at", "cart_id", "copies", "skill_id", "closure"],
    "dressers": ["name"],
    "skills": ["name"],
    "positions": ["name", "side", "x", "y", "capacity"],
    "carts": ["name", "side", "x", "y", "capacity"],
    "dresser_unavailable": ["dresser_id", "start_sec", "end_sec", "reason"],
    "tasks": ["actor_id", "from_scene_id", "to_scene_id", "exit_side",
              "position_id", "dresser_id", "start_sec", "locked", "note"],
}


def full_state():
    db.sync_item_copies(PID)
    state = db.load_state(PID)
    sched = scheduler.compute_schedule(state)
    state["schedule"] = sched
    state["runs"] = db.list_runs(PID)
    state["understudy_branches"] = db.list_branches(PID)
    if SELECTED_TURNAROUND["id"]:
        tr = db.get_turnaround(SELECTED_TURNAROUND["id"])
        state["turnaround_detail"] = turnaround.detail(state, tr) if tr else None
    else:
        state["turnaround_detail"] = None
    return state


SELECTED_TURNAROUND = {"id": None}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    return jsonify(full_state())


@app.post("/api/reschedule")
def api_reschedule():
    """重排：未锁任务按计算结果写回 start_sec，已锁任务不动。"""
    state = db.load_state(PID)
    sched = scheduler.compute_schedule(state)
    con = db.connect()
    try:
        for t in state["tasks"]:
            if t["locked"]:
                continue
            w = sched["windows"].get(t["id"])
            if w:
                con.execute("UPDATE tasks SET start_sec=? WHERE id=?", (int(w["start"]), t["id"]))
        con.commit()
    finally:
        con.close()
    out = full_state()
    out["suggestions"] = scheduler.suggest(db.load_state(PID))
    return jsonify(out)


@app.post("/api/tasks/<int:tid>")
def api_task_update(tid):
    data = request.get_json(force=True)
    con = db.connect()
    try:
        cur = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not cur:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        # 已锁节点不得移动：普通更新不得改动 start_sec / position_id（解锁同请求除外）
        if cur["locked"] and data.get("locked") != 0:
            for k in ("start_sec", "position_id"):
                if k in data and data[k] != cur[k]:
                    return jsonify({"ok": False,
                                    "error": "已锁节点不得移动：请先解锁再调整"}), 409
        sets, args = [], []
        for k in ("start_sec", "position_id", "dresser_id", "locked", "note",
                  "exit_side", "from_scene_id", "to_scene_id"):
            if k in data:
                sets.append(f"{k}=?")
                args.append(data[k])
        if not sets:
            return jsonify({"ok": False}), 400
        args.append(tid)
        con.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", args)
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.post("/api/apply_suggestion")
def api_apply_suggestion():
    """统一建议格式：{task_id, changes:{start_sec?,position_id?},
    staff_changes:[{kind,item_id,seq,dresser_ids}]}。
    未提供的字段保持不变；已锁任务/已锁分工拒绝。"""
    data = request.get_json(force=True)
    tid = int(data["task_id"])
    ch = data.get("changes") or {}
    staff_changes = data.get("staff_changes", [])
    con = db.connect()
    try:
        cur = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not cur:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        # 任务时间锁只阻挡时刻/换装位改动；纯换人由动作分工锁（下）单独把关
        if cur["locked"] and ("start_sec" in ch or "position_id" in ch):
            return jsonify({"ok": False, "error": "已锁节点不得移动"}), 409
        # 动作级分工替代排法（不动已锁分工）
        for sc in staff_changes:
            if _staff_row_locked(con, tid, sc["kind"], sc.get("item_id"), sc.get("seq", 0)):
                return jsonify({"ok": False, "error": "该动作分工已锁，不能替换"}), 409
            _replace_action_staff(con, tid, sc["kind"], sc.get("item_id"),
                                  sc.get("seq", 0), sc.get("dresser_ids", []), PID)
        sets, args = [], []
        if "start_sec" in ch:
            sets.append("start_sec=?")
            args.append(ch["start_sec"])
        if "position_id" in ch:
            sets.append("position_id=?")
            args.append(ch["position_id"])
        if sets:
            args.append(tid)
            con.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", args)
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


def _staff_row_locked(con, tid, kind, item_id, seq):
    row = con.execute(
        "SELECT 1 FROM action_staff WHERE task_id=? AND kind=? "
        "AND COALESCE(item_id,-1)=COALESCE(?,-1) AND seq=? AND locked=1 LIMIT 1",
        (tid, kind, item_id, seq)).fetchone()
    return row is not None


def _replace_action_staff(con, tid, kind, item_id, seq, dresser_ids, pid,
                          lead_id=None):
    """整体替换某动作分工（同一请求内）。"""
    con.execute(
        "DELETE FROM action_staff WHERE task_id=? AND kind=? "
        "AND COALESCE(item_id,-1)=COALESCE(?,-1) AND seq=?",
        (tid, kind, item_id, seq))
    seen = set()
    for did in dresser_ids:
        did = int(did)
        if did in seen:
            continue
        seen.add(did)
        is_lead = 1 if (lead_id or dresser_ids[0]) == did else 0
        con.execute(
            "INSERT INTO action_staff(production_id,task_id,kind,item_id,seq,"
            "dresser_id,is_lead,locked,created_at) VALUES(?,?,?,?,?,?,?,0,?)",
            (pid, tid, kind, item_id, seq, did, is_lead, time.time()))


@app.post("/api/tasks/<int:tid>/action_spec")
def api_action_spec(tid):
    """设置某穿脱动作的所需技能与人数。"""
    data = request.get_json(force=True)
    kind = data.get("kind")
    if kind not in ("don", "doff", "fetch", "walk"):
        return jsonify({"ok": False, "error": "动作类型必须是 don/doff/fetch/walk"}), 400
    item_id = data.get("item_id")
    seq = int(data.get("seq", 0))
    required = int(data.get("required_count", 1))
    if required < 1:
        return jsonify({"ok": False, "error": "所需人数至少 1 人"}), 400
    skill_id = data.get("skill_id")
    con = db.connect()
    try:
        if not con.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        row = con.execute(
            "SELECT id FROM action_specs WHERE task_id=? AND kind=? "
            "AND COALESCE(item_id,-1)=COALESCE(?,-1) AND seq=?",
            (tid, kind, item_id, seq)).fetchone()
        if row:
            con.execute("UPDATE action_specs SET skill_id=?, required_count=? WHERE id=?",
                        (skill_id, required, row["id"]))
        else:
            con.execute(
                "INSERT INTO action_specs(production_id,task_id,kind,item_id,seq,"
                "skill_id,required_count) VALUES(?,?,?,?,?,?,?)",
                (PID, tid, kind, item_id, seq, skill_id, required))
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.post("/api/tasks/<int:tid>/action_staff")
def api_action_staff(tid):
    """登记某动作分工：{kind,item_id,seq,dresser_ids,lead_id,locked}。
    已锁分工需同请求解锁（locked=0）才能改。"""
    data = request.get_json(force=True)
    kind = data.get("kind")
    if kind not in ("don", "doff", "fetch", "walk"):
        return jsonify({"ok": False, "error": "动作类型必须是 don/doff/fetch/walk"}), 400
    item_id = data.get("item_id")
    seq = int(data.get("seq", 0))
    ids = [int(x) for x in data.get("dresser_ids", [])]
    lead = data.get("lead_id")
    lead = int(lead) if lead is not None else (ids[0] if ids else None)
    locked = 1 if data.get("locked") else 0
    con = db.connect()
    try:
        t = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        existing = con.execute(
            "SELECT * FROM action_staff WHERE task_id=? AND kind=? "
            "AND COALESCE(item_id,-1)=COALESCE(?,-1) AND seq=?",
            (tid, kind, item_id, seq)).fetchall()
        was_locked = any(r["locked"] for r in existing)
        old_ids = sorted(r["dresser_id"] for r in existing)
        old_lead = next((r["dresser_id"] for r in existing if r["is_lead"]), None)
        content_changed = (old_ids != sorted(ids)) or (old_lead != lead)
        # 已锁分工：只有显式解锁请求可改；纯重复上锁（内容不变）允许
        if was_locked and content_changed and not data.get("unlock"):
            return jsonify({"ok": False,
                            "error": "该动作分工已锁（连排确认）：请先解锁"}), 409
        valid = {r["id"] for r in con.execute(
            "SELECT id FROM dressers WHERE production_id=?", (PID,)).fetchall()}
        if any(x not in valid for x in ids):
            return jsonify({"ok": False, "error": "存在不属于本剧目的服装师"}), 400
        if lead is not None and lead not in ids:
            return jsonify({"ok": False, "error": "固定负责人必须在参与者中"}), 400
        _replace_action_staff(con, tid, kind, item_id, seq, ids, PID, lead_id=lead)
        if locked:
            con.execute(
                "UPDATE action_staff SET locked=1 WHERE task_id=? AND kind=? "
                "AND COALESCE(item_id,-1)=COALESCE(?,-1) AND seq=?",
                (tid, kind, item_id, seq))
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


def _mark_action_review(con, task_ids, reason):
    """把给定任务的所有已登记动作分工标记为待复核。"""
    if not task_ids:
        return
    rows = con.execute(
        f"SELECT DISTINCT task_id,kind,item_id,seq FROM action_staff "
        f"WHERE task_id IN ({','.join('?' * len(task_ids))})", tuple(task_ids))
    for r in rows:
        exists = con.execute(
            "SELECT 1 FROM action_reviews WHERE production_id=? AND task_id=? AND kind=? "
            "AND COALESCE(item_id,-1)=COALESCE(?,-1) AND seq=?",
            (PID, r["task_id"], r["kind"], r["item_id"], r["seq"])).fetchone()
        if not exists:
            con.execute(
                "INSERT INTO action_reviews(production_id,task_id,kind,item_id,seq,"
                "reason,created_at) VALUES(?,?,?,?,?,?,?)",
                (PID, r["task_id"], r["kind"], r["item_id"], r["seq"], reason, time.time()))


@app.post("/api/dressers/<int:did>/profile")
def api_dresser_profile(did):
    """服装师资料：技能（skill_ids）、可支援侧台（sides）。
    资料变化只标记该服装师实际参与的具体动作待复核（不整任务标红）。"""
    data = request.get_json(force=True)
    con = db.connect()
    try:
        if not con.execute("SELECT 1 FROM dressers WHERE id=? AND production_id=?",
                           (did, PID)).fetchone():
            return jsonify({"ok": False, "error": "服装师不存在"}), 404
        old_skills = {r["skill_id"] for r in con.execute(
            "SELECT skill_id FROM dresser_skills WHERE dresser_id=?", (did,))}
        old_sides = {r["side"] for r in con.execute(
            "SELECT side FROM dresser_sides WHERE dresser_id=?", (did,))}
        if "skill_ids" in data:
            con.execute("DELETE FROM dresser_skills WHERE dresser_id=?", (did,))
            for sid in set(int(x) for x in data["skill_ids"]):
                con.execute("INSERT OR IGNORE INTO dresser_skills(dresser_id,skill_id) "
                            "VALUES(?,?)", (did, sid))
        if "sides" in data:
            new_sides = set(data["sides"]) & {"L", "R"}
            con.execute("DELETE FROM dresser_sides WHERE dresser_id=?", (did,))
            for sd in new_sides:
                con.execute("INSERT OR IGNORE INTO dresser_sides(dresser_id,side) "
                            "VALUES(?,?)", (did, sd))
        else:
            new_sides = old_sides
        new_skills = {r["skill_id"] for r in con.execute(
            "SELECT skill_id FROM dresser_skills WHERE dresser_id=?", (did,))}
        skill_changed = ("skill_ids" in data and new_skills != old_skills)
        side_changed = ("sides" in data and new_sides != old_sides)
        # 只标记资格状态真正翻转的具体动作（合格↔不合格），不整任务标红
        if skill_changed or side_changed:
            pre = db.load_state(PID)
            affected = scheduler.staff_affected_keys(
                pre, did, old_skills, new_skills, old_sides, new_sides)
            for tid, kind, item_id, seq, now_bad in affected:
                # 只有失去资格（合格→不合格）才需要复核；获得资格不阻断计划
                if not now_bad:
                    continue
                exists = con.execute(
                    "SELECT 1 FROM action_reviews WHERE production_id=? AND task_id=? "
                    "AND kind=? AND COALESCE(item_id,-1)=COALESCE(?,-1) AND seq=?",
                    (PID, tid, kind, item_id, seq)).fetchone()
                reason = "服装师失去所需" + \
                    ("技能/侧台资格" if skill_changed and side_changed
                     else "技能" if skill_changed else "侧台支援")
                if not exists:
                    con.execute(
                        "INSERT INTO action_reviews(production_id,task_id,kind,item_id,seq,"
                        "reason,created_at) VALUES(?,?,?,?,?,?,?)",
                        (PID, tid, kind, item_id, seq, reason, time.time()))
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.post("/api/tasks/<int:tid>/clear_review")
def api_clear_action_review(tid):
    """清除某动作的待复核标记（缺省 kind/item_id/seq 时清整任务）。"""
    data = request.get_json(silent=True) or {}
    con = db.connect()
    try:
        if "kind" in data:
            con.execute(
                "DELETE FROM action_reviews WHERE task_id=? AND kind=? "
                "AND COALESCE(item_id,-1)=COALESCE(?,-1) AND seq=?",
                (tid, data["kind"], data.get("item_id"), int(data.get("seq", 0))))
        else:
            con.execute("DELETE FROM action_reviews WHERE task_id=?", (tid,))
            con.execute("UPDATE tasks SET needs_review=0 WHERE id=?", (tid,))
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.post("/api/<entity>")
def api_create(entity):
    if entity not in FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    data = request.get_json(force=True)
    cols = [c for c in FIELDS[entity] if c in data]
    vals = [data[c] for c in cols]
    con = db.connect()
    try:
        cur = con.execute(
            f"INSERT INTO {entity}(production_id{',' if cols else ''}{','.join(cols)}) "
            f"VALUES(?{',?'*len(cols)})", [PID] + vals)
        new_id = cur.lastrowid
        # 不可用时段：只标记该人员与之时间相交的具体动作待复核
        if entity == "dresser_unavailable" and {"start_sec", "end_sec"} <= set(data):
            u0, u1 = int(data["start_sec"]), int(data["end_sec"])
            did = int(data["dresser_id"])
            if u1 <= u0:
                con.rollback()
                return jsonify({"ok": False, "error": "结束时刻必须晚于开始时刻"}), 400
            pre = db.load_state(PID)
            for tid, kind, item_id, seq in \
                    scheduler.staff_unavailable_keys(pre, did, u0, u1):
                exists = con.execute(
                    "SELECT 1 FROM action_reviews WHERE production_id=? AND task_id=? "
                    "AND kind=? AND COALESCE(item_id,-1)=COALESCE(?,-1) AND seq=?",
                    (PID, tid, kind, item_id, seq)).fetchone()
                if not exists:
                    con.execute(
                        "INSERT INTO action_reviews(production_id,task_id,kind,item_id,seq,"
                        "reason,created_at) VALUES(?,?,?,?,?,?,?)",
                        (PID, tid, kind, item_id, seq,
                         f"服装师 {scheduler.fmt(u0)}–{scheduler.fmt(u1)} 不可用",
                         time.time()))
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True, "id": new_id})


@app.post("/api/<entity>/<int:rid>")
def api_update(entity, rid):
    if entity not in FIELDS:
        return jsonify({"ok": False, "error": "unknown entity"}), 404
    data = request.get_json(force=True)
    cols = [c for c in FIELDS[entity] if c in data]
    if not cols:
        return jsonify({"ok": False}), 400
    con = db.connect()
    try:
        con.execute(f"UPDATE {entity} SET {','.join(c+'=?' for c in cols)} WHERE id=?",
                    [data[c] for c in cols] + [rid])
        # 场次时间或道具/服装状态变化 → 关联任务整体标复核，
        # 已登记动作级分工的具体动作也逐条标记
        if entity == "scenes" and {"start_sec", "duration_sec"} & set(data):
            con.execute(
                "UPDATE tasks SET needs_review=1 WHERE from_scene_id=? OR to_scene_id=?",
                (rid, rid))
            tids = [r["id"] for r in con.execute(
                "SELECT id FROM tasks WHERE from_scene_id=? OR to_scene_id=?",
                (rid, rid)).fetchall()]
            _mark_action_review(con, tids, "场次时间变更")
        if entity == "items" and {"status", "available_at", "cart_id", "skill_id"} & set(data):
            con.execute("""
              UPDATE tasks SET needs_review=1 WHERE id IN (
                SELECT t.id FROM tasks t
                JOIN looks l1 ON l1.actor_id=t.actor_id AND l1.scene_id=t.from_scene_id
                JOIN look_items li1 ON li1.look_id=l1.id AND li1.item_id=?
                UNION
                SELECT t.id FROM tasks t
                JOIN looks l2 ON l2.actor_id=t.actor_id AND l2.scene_id=t.to_scene_id
                JOIN look_items li2 ON li2.look_id=l2.id AND li2.item_id=?)
            """, (rid, rid))
            tids = [r["id"] for r in con.execute("""
                SELECT DISTINCT t.id FROM tasks t
                JOIN looks l1 ON l1.actor_id=t.actor_id AND l1.scene_id=t.from_scene_id
                JOIN look_items li1 ON li1.look_id=l1.id AND li1.item_id=?
                UNION
                SELECT t.id FROM tasks t
                JOIN looks l2 ON l2.actor_id=t.actor_id AND l2.scene_id=t.to_scene_id
                JOIN look_items li2 ON li2.look_id=l2.id AND li2.item_id=?
            """, (rid, rid)).fetchall()]
            _mark_action_review(con, tids, "服装/道具变更")
            # 替演分支：只标记基准快照含该服装的相关分支待复核
            db.mark_understudy_dirty(PID, item_ids=[rid])
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.delete("/api/<entity>/<int:rid>")
def api_delete(entity, rid):
    if entity not in FIELDS:
        return jsonify({"ok": False}), 404
    con = db.connect()
    try:
        con.execute(f"DELETE FROM {entity} WHERE id=?", (rid,))
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.post("/api/look_items")
def api_look_items():
    """设置某造型的服装清单：{look_id, item_ids:[...]}"""
    data = request.get_json(force=True)
    lid = int(data["look_id"])
    con = db.connect()
    try:
        con.execute("DELETE FROM look_items WHERE look_id=?", (lid,))
        for i, iid in enumerate(data.get("item_ids", [])):
            con.execute("INSERT OR IGNORE INTO look_items(look_id,item_id,ord) VALUES(?,?,?)",
                        (lid, int(iid), i))
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.post("/api/revisions")
def api_save_revision():
    note = (request.get_json(force=True) or {}).get("note", "")
    db.save_revision(note, PID)
    return jsonify(full_state())


@app.post("/api/revisions/<int:rid>/restore")
def api_restore_revision(rid):
    ok = db.restore_revision(rid)
    if not ok:
        return jsonify({"ok": False, "error": "修订不存在"}), 404
    return jsonify(full_state())


# ---------------- 连排实测 ----------------

def _run_detail(run_id):
    run = db.get_run(run_id)
    if not run:
        return None
    events = db.run_events(run_id)
    return {"run": {k: run[k] for k in
                    ("id", "revision_id", "name", "status", "created_at", "closed_at")},
            "plan": rehearsal.load_plan(run),
            "events": events,
            "analysis": rehearsal.analyze(run, events)}


@app.post("/api/runs")
def api_run_create():
    """从指定修订开启一次连排：冻结该修订的基准计划，不改写当前方案。"""
    data = request.get_json(force=True)
    rev = db.get_revision(int(data.get("revision_id", 0)))
    if not rev:
        return jsonify({"ok": False, "error": "基准修订不存在，请先保存修订"}), 404
    snap = json.loads(rev["snapshot"])
    plan = rehearsal.freeze_plan(db.load_state(PID), snap)
    name = data.get("name") or f"连排·修订#{rev['id']}"
    run_id = db.create_run(rev["id"], name, plan, PID)
    return jsonify({"ok": True, "id": run_id, "run": _run_detail(run_id)})


@app.get("/api/runs/<int:run_id>")
def api_run_get(run_id):
    d = _run_detail(run_id)
    if not d:
        return jsonify({"ok": False, "error": "连排不存在"}), 404
    return jsonify(d)


EVENT_KINDS = ("start", "done", "skip", "exception")


@app.post("/api/runs/<int:run_id>/events")
def api_run_event(run_id):
    """按动作打点：开始/完成/跳过/异常。异常与补正（覆盖已有打点）必须留理由。"""
    run = db.get_run(run_id)
    if not run:
        return jsonify({"ok": False, "error": "连排不存在"}), 404
    if run["status"] != "open":
        return jsonify({"ok": False, "error": "连排已结束，不能再打点"}), 409
    data = request.get_json(force=True)
    kind = data.get("kind")
    reason = (data.get("reason") or "").strip()
    if kind not in EVENT_KINDS:
        return jsonify({"ok": False, "error": "未知打点类型"}), 400
    plan = rehearsal.load_plan(run)
    try:
        tid, idx = int(data.get("task_id")), int(data.get("action_idx"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少任务或动作序号"}), 400
    act = next((a for a in plan["actions"] if a["task_id"] == tid and a["idx"] == idx), None)
    if not act:
        return jsonify({"ok": False, "error": "动作不在本次连排基准计划中"}), 404
    try:
        at_sec = int(data.get("at_sec"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "时刻必须是整数秒"}), 400
    if not 0 <= at_sec <= 86400:
        return jsonify({"ok": False, "error": "时刻超出合理范围"}), 400
    if kind == "exception" and not reason:
        return jsonify({"ok": False, "error": "异常打点必须填写理由"}), 400
    # 现场实际使用的服装/副本（可空=按计划）；类型校验，错用在分析中识别
    item_id, copy_id = data.get("item_id"), data.get("copy_id")
    if item_id is not None:
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "item_id 必须是整数"}), 400
        if item_id not in {i["id"] for i in plan["items"]}:
            return jsonify({"ok": False, "error": "服装不在本次连排基准计划中"}), 404
    if copy_id is not None:
        try:
            copy_id = int(copy_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "copy_id 必须是整数"}), 400
        if copy_id < 1:
            return jsonify({"ok": False, "error": "copy_id 必须 ≥ 1"}), 400
    # 实际参与者（可空=按冻结分工）：必须是冻结基准中的服装师
    dresser_ids = data.get("dresser_ids")
    if dresser_ids is not None:
        try:
            dresser_ids = sorted({int(x) for x in dresser_ids})
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "dresser_ids 必须是整数列表"}), 400
        valid = {d["id"] for d in plan["dressers"]}
        if any(x not in valid for x in dresser_ids):
            return jsonify({"ok": False, "error": "参与者不在本次连排基准人员中"}), 400
    events = db.run_events(run_id)
    prev = rehearsal.effective_events(events).get((tid, idx, kind))
    if prev and not reason:
        return jsonify({"ok": False,
                        "error": "补正已有打点必须填写理由（原记录保留备查）"}), 400
    db.add_event(run_id, tid, idx, kind, at_sec, reason,
                 supersedes=prev["id"] if prev else None,
                 item_id=item_id, copy_id=copy_id, dresser_ids=dresser_ids)
    return jsonify({"ok": True, "run": _run_detail(run_id)})


@app.post("/api/runs/<int:run_id>/close")
def api_run_close(run_id):
    if not db.get_run(run_id):
        return jsonify({"ok": False, "error": "连排不存在"}), 404
    db.close_run(run_id)
    return jsonify({"ok": True, "run": _run_detail(run_id)})


@app.get("/api/runs/summary")
def api_run_summary():
    """汇总已结束连排，给出服装用时建议；?revision_id= 限定同一基准修订。"""
    rid = request.args.get("revision_id", type=int)
    return jsonify({"suggestions":
                    rehearsal.summarize_suggestions(db.load_state(PID), PID, rid)})


@app.post("/api/runs/derive")
def api_run_derive():
    """勾选建议 → 从所选连排的基准修订派生新修订：
    只重排受影响任务，锁定节点不动，不触碰当前可变方案。"""
    data = request.get_json(force=True)
    try:
        run_id = int(data.get("run_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "必须指定所选连排 run_id"}), 400
    result = rehearsal.derive_revision(run_id, data.get("keys", []), PID)
    if not result:
        return jsonify({"ok": False, "error": "连排/基准修订不存在、基准数据不完整"
                                              "（旧版修订缺辅助状态），或没有可应用的建议"}), 400
    out = full_state()
    out["derived"] = result
    return jsonify(out)


# ---------------- 替演推演 ----------------

def _branch_inputs(br):
    """读取分支的卡司/改派/备注/改衣开工时刻。"""
    return (json.loads(br["cast_json"] or "{}"),
            json.loads(br["assigns_json"] or "{}"),
            json.loads(br["notes_json"] or "{}"),
            int(br.get("alter_start_sec") or 0))


def _branch_base(br):
    """分支推演用 state：基准修订快照 + 当前尺寸/副本/适配资料。"""
    state = db.load_state(PID)
    rev = db.get_revision(br["revision_id"])
    if not rev:
        return None
    return understudy.base_state_for(state, rev)


def _branch_detail(br):
    plan = understudy.parse_plan(br)
    return {"branch": {k: br[k] for k in
                       ("id", "revision_id", "name", "status", "needs_review",
                        "created_at", "confirmed_at", "alter_start_sec")},
            "cast": json.loads(br["cast_json"] or "{}"),
            "assigns": json.loads(br["assigns_json"] or "{}"),
            "notes": json.loads(br["notes_json"] or "{}"),
            "plan": plan}


def _recompute(br):
    """按分支输入重算并落盘（仅草稿）；返回 detail。"""
    base = _branch_base(br)
    if base is None:
        return None
    cast, assigns, notes, alter_start = _branch_inputs(br)
    plan = understudy.build_branch(base, cast, assigns, notes, alter_start)
    db.update_branch(br["id"], plan=plan)
    return _branch_detail(db.get_branch(br["id"]))


@app.get("/api/understudy/branches")
def api_branch_list():
    out = []
    for br in db.list_branches(PID):
        d = _branch_detail(br)
        p = d["plan"] or {}
        out.append({**d["branch"], "n_cast": len(d["cast"]),
                    "can_confirm": p.get("can_confirm", False),
                    "n_blocking": p.get("n_blocking", 0),
                    "earliest": p.get("earliest")})
    return jsonify({"branches": out})


@app.get("/api/understudy/branches/<int:bid>")
def api_branch_get(bid):
    br = db.get_branch(bid)
    if not br:
        return jsonify({"ok": False, "error": "替演分支不存在"}), 404
    return jsonify(_branch_detail(br))


@app.post("/api/understudy/branches")
def api_branch_create():
    """从任一修订开启替演分支：{revision_id, name?, cast?, alter_start_sec?}。"""
    data = request.get_json(force=True)
    rev = db.get_revision(int(data.get("revision_id", 0)))
    if not rev:
        return jsonify({"ok": False, "error": "基准修订不存在，请先保存修订"}), 404
    cast = {str(k): int(v) for k, v in (data.get("cast") or {}).items() if v}
    plan = understudy.build_branch(
        understudy.base_state_for(db.load_state(PID), rev),
        cast, {}, {}, int(data.get("alter_start_sec") or 0))
    name = (data.get("name") or f"替演·修订#{rev['id']}").strip()
    bid = db.create_branch(rev["id"], name, cast, {}, {},
                           int(data.get("alter_start_sec") or 0), plan, PID)
    return jsonify({"ok": True, "id": bid, "detail": _branch_detail(db.get_branch(bid))})


@app.post("/api/understudy/branches/<int:bid>/cast")
def api_branch_cast(bid):
    """拖换卡司：{task_id, actor_id}（actor_id 空=还原原角）；整体重算。"""
    br = db.get_branch(bid)
    if not br:
        return jsonify({"ok": False, "error": "替演分支不存在"}), 404
    if br["status"] == "confirmed":
        return jsonify({"ok": False, "error": "确认版已冻结，不能再拖换卡司"}), 409
    data = request.get_json(force=True)
    try:
        tid, aid = str(int(data["task_id"])), int(data.get("actor_id") or 0)
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "task_id/actor_id 无效"}), 400
    cast, assigns, notes, alter_start = _branch_inputs(br)
    # 任务必须属于基准修订快照：当前方案在该修订之后新增的任务不能换角
    base = _branch_base(br)
    if base is None:
        return jsonify({"ok": False, "error": "基准修订不存在"}), 400
    snap_ids = {str(t["id"]) for t in base["tasks"]}
    if tid not in snap_ids:
        return jsonify({"ok": False,
                        "error": f"任务#{tid}不在基准修订中（可能是修订后新增），"
                                 "不能在该分支换角"}), 400
    if aid and aid not in {a["id"] for a in base["actors"]}:
        return jsonify({"ok": False, "error": "候补演员不存在"}), 400
    if aid:
        cast[tid] = aid
    else:
        cast.pop(tid, None)
        # 还原原角时清掉该任务的改派/备注
        for k in [k for k in assigns if k.startswith(tid + ":")]:
            assigns.pop(k, None)
        for k in [k for k in notes if k.startswith(tid + ":")]:
            notes.pop(k, None)
    db.update_branch(bid, cast=cast, assigns=assigns, notes=notes)
    detail = _recompute(db.get_branch(bid))
    return jsonify({"ok": True, "detail": detail, "state": full_state()})


@app.post("/api/understudy/branches/<int:bid>/assign")
def api_branch_assign(bid):
    """人工改派副本：{task_id,item_id,copy_id(=item_copies.id),note}。
    边界尺寸/人工改派必须备注：note 为空直接拒绝。"""
    br = db.get_branch(bid)
    if not br:
        return jsonify({"ok": False, "error": "替演分支不存在"}), 404
    if br["status"] == "confirmed":
        return jsonify({"ok": False, "error": "确认版已冻结，不能再改派副本"}), 409
    data = request.get_json(force=True)
    try:
        tid, iid, cid = int(data["task_id"]), int(data["item_id"]), int(data["copy_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "task_id/item_id/copy_id 无效"}), 400
    note = (data.get("note") or "").strip()
    cast, assigns, notes, alter_start = _branch_inputs(br)
    base = _branch_base(br)
    if str(tid) not in {str(t["id"]) for t in base["tasks"]}:
        return jsonify({"ok": False,
                        "error": f"任务#{tid}不在基准修订中，不能改派副本"}), 400
    con = db.connect()
    try:
        row = con.execute("SELECT id FROM item_copies WHERE id=? AND item_id=?",
                          (cid, iid)).fetchone()
    finally:
        con.close()
    if not row:
        return jsonify({"ok": False, "error": "该副本不属于所选服装"}), 400
    key = f"{tid}:{iid}"
    assigns[key] = cid
    if note:
        notes[key] = note
    elif not (notes.get(key) or "").strip():
        return jsonify({"ok": False,
                        "error": "人工改派副本必须填写备注（理由/边界尺寸）"}), 400
    db.update_branch(bid, assigns=assigns, notes=notes)
    detail = _recompute(db.get_branch(bid))
    return jsonify({"ok": True, "detail": detail, "state": full_state()})


@app.post("/api/understudy/branches/<int:bid>/note")
def api_branch_note(bid):
    """登记/修改适配备注：{task_id,item_id,note}（清空=删除）。"""
    br = db.get_branch(bid)
    if not br:
        return jsonify({"ok": False, "error": "替演分支不存在"}), 404
    if br["status"] == "confirmed":
        return jsonify({"ok": False, "error": "确认版已冻结，备注不可修改"}), 409
    data = request.get_json(force=True)
    key = f"{int(data['task_id'])}:{int(data['item_id'])}"
    cast, assigns, notes, alter_start = _branch_inputs(br)
    base = _branch_base(br)
    if not str(key.split(":")[0]) in {str(t["id"]) for t in base["tasks"]}:
        return jsonify({"ok": False, "error": "任务不在基准修订中，不能登记备注"}), 400
    note = (data.get("note") or "").strip()
    if note:
        notes[key] = note
    else:
        notes.pop(key, None)
    db.update_branch(bid, notes=notes)
    detail = _recompute(db.get_branch(bid))
    return jsonify({"ok": True, "detail": detail, "state": full_state()})


@app.post("/api/understudy/branches/<int:bid>/alter_start")
def api_branch_alter_start(bid):
    """设置改衣可开始的演出时钟时刻（秒）。"""
    br = db.get_branch(bid)
    if not br:
        return jsonify({"ok": False, "error": "替演分支不存在"}), 404
    if br["status"] == "confirmed":
        return jsonify({"ok": False, "error": "确认版已冻结"}), 409
    sec = int((request.get_json(force=True) or {}).get("alter_start_sec") or 0)
    db.update_branch(bid, alter_start_sec=sec)
    detail = _recompute(db.get_branch(bid))
    return jsonify({"ok": True, "detail": detail, "state": full_state()})


@app.post("/api/understudy/branches/<int:bid>/recompute")
def api_branch_recompute(bid):
    """资料变化后手动重新推演（草稿）；已确认版只返回冻结计划。"""
    br = db.get_branch(bid)
    if not br:
        return jsonify({"ok": False, "error": "替演分支不存在"}), 404
    if br["status"] == "confirmed":
        return jsonify({"ok": True, "detail": _branch_detail(br), "frozen": True})
    detail = _recompute(br)
    if detail is None:
        return jsonify({"ok": False, "error": "基准修订不存在"}), 400
    db.update_branch(bid, needs_review=False)
    detail = _branch_detail(db.get_branch(bid))
    return jsonify({"ok": True, "detail": detail, "state": full_state()})


@app.post("/api/understudy/branches/<int:bid>/clear_review")
def api_branch_clear_review(bid):
    br = db.get_branch(bid)
    if not br:
        return jsonify({"ok": False, "error": "替演分支不存在"}), 404
    db.update_branch(bid, needs_review=False)
    return jsonify({"ok": True, "detail": _branch_detail(db.get_branch(bid))})


@app.post("/api/understudy/branches/<int:bid>/confirm")
def api_branch_confirm(bid):
    """确认版冻结：卡司、适配决定、受影响任务。存在任一阻断（含未备注决定）
    时禁止确认。"""
    br = db.get_branch(bid)
    if not br:
        return jsonify({"ok": False, "error": "替演分支不存在"}), 404
    if br["status"] == "confirmed":
        return jsonify({"ok": False, "error": "该分支已是确认版"}), 409
    detail = _recompute(br)
    if detail is None:
        return jsonify({"ok": False, "error": "基准修订不存在"}), 400
    plan = detail["plan"]
    if not plan.get("can_confirm"):
        e = plan.get("earliest") or {}
        return jsonify({"ok": False,
                        "error": f"尚有阻断未解决，不能确认：[{scheduler.fmt(e.get('time', 0))}] "
                                 f"{e.get('message', '')}"}), 409
    db.confirm_branch(bid, plan)
    return jsonify({"ok": True, "detail": _branch_detail(db.get_branch(bid)),
                    "state": full_state()})


@app.delete("/api/understudy/branches/<int:bid>")
def api_branch_delete(bid):
    br = db.get_branch(bid)
    if not br:
        return jsonify({"ok": False, "error": "替演分支不存在"}), 404
    if br["status"] == "confirmed":
        return jsonify({"ok": False, "error": "确认版已冻结，不能删除"}), 409
    db.delete_branch(bid)
    return jsonify({"ok": True, "state": full_state()})


# ---------------- 替演资料登记 ----------------

@app.post("/api/actors/<int:aid>/measures")
def api_actor_measures(aid):
    """演员关键尺寸（身高/胸围/腰围/臀围/肩宽/脚长，厘米）。变化时标记相关分支。"""
    data = request.get_json(force=True)
    con = db.connect()
    try:
        if not con.execute("SELECT 1 FROM actors WHERE id=? AND production_id=?",
                           (aid, PID)).fetchone():
            return jsonify({"ok": False, "error": "演员不存在"}), 404
        row = con.execute("SELECT * FROM actor_measures WHERE actor_id=?", (aid,)).fetchone()
        vals = {k: (float(data[k]) if data.get(k) not in (None, "") else None)
                for k in ("height", "chest", "waist", "hip", "shoulder", "foot")
                if k in data}
        if row is None:
            cols = ["production_id", "actor_id", "updated_at"] + list(vals)
            qs = ",".join("?" for _ in cols)
            con.execute(f"INSERT INTO actor_measures({','.join(cols)}) VALUES({qs})",
                        [PID, aid, time.time()] + list(vals.values()))
        else:
            sets = [f"{k}=?" for k in vals]
            con.execute(f"UPDATE actor_measures SET {','.join(sets)}, updated_at=? "
                        "WHERE actor_id=?", list(vals.values()) + [time.time(), aid])
        con.commit()
    finally:
        con.close()
    db.mark_understudy_dirty(PID, actor_ids=[aid])
    return jsonify(full_state())


@app.post("/api/understudy/roster")
def api_roster_set():
    """登记角色候补顺位：{role_actor_id, under_actor_id, priority}。"""
    data = request.get_json(force=True)
    try:
        role, under, prio = int(data["role_actor_id"]), int(data["under_actor_id"]), \
            int(data.get("priority") or 1)
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "参数无效"}), 400
    con = db.connect()
    try:
        ids = {r["id"] for r in con.execute(
            "SELECT id FROM actors WHERE production_id=?", (PID,)).fetchall()}
        if role not in ids or under not in ids:
            return jsonify({"ok": False, "error": "演员不存在"}), 400
        con.execute(
            "INSERT INTO understudy_roster(production_id,role_actor_id,under_actor_id,priority) "
            "VALUES(?,?,?,?) ON CONFLICT(production_id,role_actor_id,under_actor_id) "
            "DO UPDATE SET priority=excluded.priority", (PID, role, under, prio))
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.delete("/api/understudy/roster/<int:rid>")
def api_roster_delete(rid):
    con = db.connect()
    try:
        con.execute("DELETE FROM understudy_roster WHERE id=? AND production_id=?", (rid, PID))
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.post("/api/item_copies/<int:cid>")
def api_copy_update(cid):
    """实物副本：标签、闭合件类型（''=服装默认）。"""
    data = request.get_json(force=True)
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM item_copies WHERE id=? AND production_id=?",
                          (cid, PID)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "副本不存在"}), 404
        sets, args = [], []
        if "label" in data:
            sets.append("label=?")
            args.append(str(data["label"]))
        if "closure" in data:
            cl = str(data.get("closure") or "")
            if cl and cl not in understudy.CLOSURE_EXTRA:
                return jsonify({"ok": False, "error": "闭合件类型无效"}), 400
            sets.append("closure=?")
            args.append(cl)
        if sets:
            con.execute(f"UPDATE item_copies SET {','.join(sets)} WHERE id=?", args + [cid])
            con.commit()
            iid = row["item_id"]
    finally:
        con.close()
    db.mark_understudy_dirty(PID, item_ids=[iid])
    return jsonify(full_state())


@app.post("/api/item_copies/<int:cid>/fit")
def api_copy_fit(cid):
    """副本适配区间：{dim, lo, hi, alterable, alter_sec}；hi 空=删除该部位区间。"""
    data = request.get_json(force=True)
    dim = data.get("dim")
    if dim not in understudy.DIM_KEYS:
        return jsonify({"ok": False, "error": "部位无效"}), 400
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM item_copies WHERE id=? AND production_id=?",
                          (cid, PID)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "副本不存在"}), 404
        if data.get("hi") in (None, "") or data.get("lo") in (None, ""):
            con.execute("DELETE FROM copy_fit WHERE copy_id=? AND dim=?", (cid, dim))
        else:
            lo, hi = float(data["lo"]), float(data["hi"])
            if hi < lo:
                return jsonify({"ok": False, "error": "适配上限不能小于下限"}), 400
            alt = 1 if data.get("alterable") else 0
            alt_sec = max(0, int(data.get("alter_sec") or 0))
            con.execute(
                "INSERT INTO copy_fit(production_id,copy_id,dim,lo,hi,alterable,alter_sec) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(copy_id,dim) DO UPDATE SET "
                "lo=excluded.lo,hi=excluded.hi,alterable=excluded.alterable,"
                "alter_sec=excluded.alter_sec",
                (PID, cid, dim, lo, hi, alt, alt_sec))
        con.commit()
        iid = row["item_id"]
    finally:
        con.close()
    db.mark_understudy_dirty(PID, item_ids=[iid])
    return jsonify(full_state())


@app.get("/export/understudy/<int:bid>/sheet")
def export_understudy_sheet(bid):
    """替演换装单（HTML，可打印）：冻结卡司、适配决定、改衣、逐任务动作。"""
    br = db.get_branch(bid)
    if not br:
        return "替演分支不存在", 404
    detail = _branch_detail(br)
    plan = detail["plan"]
    if not plan:
        return "分支尚未推演", 400
    state = _branch_base(br)
    actors = {a["id"]: a for a in state["actors"]}
    scenes = {s["id"]: s for s in state["scenes"]}
    items = {i["id"]: i for i in state["items"]}
    positions = {p["id"]: p for p in state["positions"]}
    dressers = {d["id"]: d for d in state["dressers"]}
    orig_tasks = {t["id"]: t for t in state["tasks"]}
    cast = {int(k): v for k, v in plan["cast"].items()}
    rows = []
    for tid in sorted(cast, key=lambda x: plan["windows"].get(str(x), {}).get("start", 0)):
        w = plan["windows"].get(str(tid))
        ot = orig_tasks.get(tid)
        if not w or not ot:
            continue
        fs, ts = scenes.get(ot["from_scene_id"]), scenes.get(ot["to_scene_id"])
        pos = positions.get(ot["position_id"])
        acts = sorted((a for a in plan["actions"] if a["task_id"] == tid),
                      key=lambda a: a["action_idx"])
        parts = []
        for a in acts:
            who = "、".join(dressers[d]["name"] for d in a["staff_ids"] if d in dressers)
            parts.append(f"{a['label']}({a['dur']}s" + (f"·{who}" if who else "") + ")")
        rows.append(
            f"<tr><td>{scheduler.fmt(w['start'])}</td>"
            f"<td>#{tid} {actors.get(cast[tid], {}).get('name', '?')}"
            f"<span class=sub>（替 {actors.get(ot['actor_id'], {}).get('name', '?')}）</span></td>"
            f"<td>{fs['name'] if fs else ''} → {ts['name'] if ts else ''}</td>"
            f"<td>{pos['name'] if pos else '-'}</td>"
            f"<td>{scheduler.fmt(w['deadline'])}</td>"
            f"<td class=acts>{' → '.join(parts)}</td>"
            f"<td>{'按时' if w['ok'] else '<b class=bad>超时</b>'}</td></tr>")
    fit_rows = "".join(
        f"<tr><td>#{f['task_id']}</td><td>{f['actor_name']}</td>"
        f"<td>{items[f['item_id']]['name'] if f['item_id'] in items else f['item_id']}</td>"
        f"<td>第{f['copy_no']}件</td><td><b class='{f['status']}'>{f['status']}</b></td>"
        f"<td>{'、'.join(understudy.dim_cn(d) for d in f['boundary']) or '-'}</td>"
        f"<td>{detail['notes'].get(str(f['task_id'])+':'+str(f['item_id']), '')}</td></tr>"
        for f in plan["fit_rows"])
    dec_rows = "".join(
        f"<li>{d['reason']}"
        + (f"｜改衣最晚 {scheduler.fmt(d['latest_alter_start'])} 开工"
           if d["kind"] == "alter" and "latest_alter_start" in d else "")
        + (f"｜备注：{detail['notes'].get(str(d['task_id'])+':'+str(d['item_id']), '')}" or "")
        + "</li>" for d in plan["decisions"]) or "<li>无</li>"
    conf_rows = "".join(
        f"<li>[{scheduler.fmt(c['time'])}] 任务#{c['task_id']} {c['message']}</li>"
        for c in plan["conflicts"]) or "<li>无 ✓</li>"
    status = "已确认（冻结）" if br["status"] == "confirmed" else "草稿"
    html = f"""<!doctype html><html lang=zh><meta charset=utf-8>
<title>替演换装单 · {_xml(br['name'])}</title>
<style>body{{font-family:sans-serif;margin:22px}}table{{border-collapse:collapse;width:100%;margin:8px 0}}
td,th{{border:1px solid #999;padding:5px 8px;font-size:12px;vertical-align:top}}
h1{{font-size:19px}}h2{{font-size:15px;margin-top:18px}}.sub{{color:#888;font-size:11px}}
.bad{{color:#c0392b}}.合身{{color:#27ae60}}.需改衣{{color:#b9770e}}.越界{{color:#c0392b}}
.acts{{font-size:11px;color:#555}}</style>
<h1>替演换装单 · {_xml(br['name'])}</h1>
<p>基准修订 #{br['revision_id']}｜状态：{status}｜改衣可开工 {scheduler.fmt(plan['alter_start_sec'])}
｜{'可确认 ✓' if plan['can_confirm'] else '<b class=bad>有阻断，未确认</b>'}</p>
<h2>换装任务（冻结卡司与分工）</h2>
<table><tr><th>开始</th><th>候补演员</th><th>场次</th><th>换装位</th><th>开场截止</th>
<th>动作顺序</th><th>结果</th></tr>{''.join(rows)}</table>
<h2>副本适配决定</h2>
<table><tr><th>任务</th><th>候补</th><th>服装</th><th>副本</th><th>判定</th>
<th>边界部位</th><th>备注</th></tr>{fit_rows}</table>
<h2>改衣/边界/人工改派</h2><ul>{dec_rows}</ul>
<h2>冲突（冻结时状态）</h2><ul>{conf_rows}</ul>"""
    return html


def _understudy_diff_svg(br, plan, state, dl=False):
    """原计划 vs 替演 叠放 SVG。两层窗口分别取分支冻结的 orig_windows 与
    windows（同一基准修订的场次/任务），真实反映换角前后的起止差异；不读当前
    方案坐标，也不重新计算原计划。"""
    scenes = plan.get("base_scenes") or state["scenes"]
    base_tasks = plan.get("base_tasks") or []
    orig_windows = plan.get("orig_windows") or {}
    cast = {int(k): v for k, v in plan["cast"].items()}
    orig_task = {t["id"]: t for t in base_tasks}
    actor_ids = sorted({cast[tid] for tid in cast if tid in orig_task} |
                       {orig_task[tid]["actor_id"] for tid in cast if tid in orig_task})
    actors = {a["id"]: a for a in state["actors"]}
    total = max((s["start_sec"] + s["duration_sec"] for s in scenes), default=600) + 60
    scale = 900.0 / max(total, 1)
    lane_h, top = 52, 34
    h = top + lane_h * max(1, len(actor_ids)) + 46
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{h}" '
             f'font-family="sans-serif" font-size="11">',
             f'<rect width="960" height="{h}" fill="#fff"/>']
    for s in scenes:
        x = 40 + s["start_sec"] * scale
        parts.append(f'<rect x="{x:.1f}" y="8" width="{s["duration_sec"]*scale:.1f}" '
                     f'height="14" fill="#dde6f2"/>')
        parts.append(f'<text x="{x+2:.1f}" y="19" fill="#334">{_xml(s["name"])}</text>')
    for i, aid in enumerate(actor_ids):
        y = top + i * lane_h
        parts.append(f'<text x="2" y="{y+26}">'
                     f'{_xml(actors.get(aid, {}).get("name", "#"+str(aid)))}</text>')
        parts.append(f'<line x1="40" y1="{y+lane_h}" x2="950" y2="{y+lane_h}" stroke="#eee"/>')

    def bar(tid, w, y, color, label):
        bx = 40 + w["start"] * scale
        wdt = max(3, (w["end"] - w["start"]) * scale)
        parts.append(f'<rect x="{bx:.1f}" y="{y}" width="{wdt:.1f}" height="13" rx="2" '
                     f'fill="{color}" opacity="0.9"/>')
        parts.append(f'<text x="{bx+2:.1f}" y="{y+10}" fill="#fff" font-size="9">'
                     f'#{tid}{label}</text>')
        dx = 40 + w["deadline"] * scale
        parts.append(f'<line x1="{dx:.1f}" y1="{y-2}" x2="{dx:.1f}" y2="{y+17}" '
                     f'stroke="#c0392b" stroke-dasharray="3 2"/>')

    # 上排：原计划（orig_windows）；下排：替演重排（windows），同一演员泳道
    for i, aid in enumerate(actor_ids):
        y = top + i * lane_h
        for tid, ot in orig_task.items():
            if ot["actor_id"] != aid or str(tid) in {str(k) for k in cast}:
                continue
            w = orig_windows.get(str(tid))
            if w:
                bar(tid, w, y + 4, "#95a5a6", "原")
        # 被换角任务：先画上排原角的原计划窗口
        for tid, ua in cast.items():
            ot = orig_task.get(tid)
            if not ot:
                continue
            if ot["actor_id"] == aid:
                w = orig_windows.get(str(tid))
                if w:
                    bar(tid, w, y + 4, "#7f8c8d", "原")
            if ua == aid:
                w = plan["windows"].get(str(tid))
                if w:
                    color = "#c0392b" if not w["ok"] else "#8e44ad"
                    bar(tid, w, y + 21, color, "替")
    # 冲突三角：橙色为最早，其余红色
    for i, c in enumerate(plan["conflicts"]):
        aid = cast.get(c["task_id"])
        if aid is None or aid not in actor_ids:
            ot = orig_task.get(c["task_id"])
            aid = ot["actor_id"] if ot else None
        if aid not in actor_ids:
            continue
        y = top + actor_ids.index(aid) * lane_h
        cx = 40 + c["time"] * scale
        parts.append(f'<path d="M{cx:.1f},{y+40} l4,-6 l4,6 z" '
                     f'fill={"#e67e22" if i == 0 else "#e74c3c"}">'
                     f'<title>{_xml(c["message"])}</title></path>')
    ly = h - 28
    parts.append(f'<rect x="40" y="{ly}" width="12" height="12" fill="#7f8c8d"/>'
                 f'<text x="56" y="{ly+10}">原计划</text>'
                 f'<rect x="110" y="{ly}" width="12" height="12" fill="#8e44ad"/>'
                 f'<text x="126" y="{ly+10}">替演(按时)</text>'
                 f'<rect x="200" y="{ly}" width="12" height="12" fill="#c0392b"/>'
                 f'<text x="216" y="{ly+10}">替演(超时)</text>'
                 f'<path d="M300,{ly+2} l4,-7 l4,7 z" fill="#e67e22"/>'
                 f'<text x="312" y="{ly+10}">最早冲突</text>')
    parts.append(f'<text x="40" y="{h-8}" fill="#888" font-size="10">'
                 f'{_xml(br["name"])} · 基准修订#{br["revision_id"]}</text>')
    parts.append("</svg>")
    svg = "".join(parts)
    if dl:
        return Response(svg, mimetype="image/svg+xml",
                        headers={"Content-Disposition":
                                 f"attachment; filename=understudy{bid}_diff.svg"})
    return Response(svg, mimetype="image/svg+xml")


@app.get("/export/understudy/<int:bid>/diff.svg")
def export_understudy_diff(bid):
    """原计划—替演差异 SVG（叠放）。"""
    br = db.get_branch(bid)
    if not br:
        return "替演分支不存在", 404
    plan = understudy.parse_plan(br)
    if not plan:
        return "分支尚未推演", 400
    state = db.load_state(PID)
    return _understudy_diff_svg(br, plan, state,
                                dl=bool(request.args.get("dl")))


# ---------------- 场间复位工作区 ----------------

def _tr_detail_or_404(tid):
    tr = db.get_turnaround(tid)
    if not tr or tr["production_id"] != PID:
        return None, (jsonify({"ok": False, "error": "复位工作区不存在"}), 404)
    return tr, None


@app.post("/api/turnarounds")
def api_turnaround_create():
    """从选定连排的实际穿用记录或已确认方案（修订）生成逐副本养护路线。"""
    data = request.get_json(force=True)
    source_kind = data.get("source_kind")
    if source_kind not in ("run", "revision"):
        return jsonify({"ok": False, "error": "来源必须是 run（连排）或 revision（修订）"}), 400
    try:
        source_id = int(data.get("source_id"))
        offset = max(0, int(data.get("evening_offset_sec") or 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "来源/晚场偏移参数无效"}), 400
    if source_kind == "run":
        run = db.get_run(source_id)
        if not run or run["production_id"] != PID:
            return jsonify({"ok": False, "error": "连排不存在"}), 404
        src_name = run["name"]
    else:
        rev = db.get_revision(source_id)
        if not rev or rev["production_id"] != PID:
            return jsonify({"ok": False, "error": "修订不存在"}), 404
        src_name = f"修订#{rev['id']}"
    state = db.load_state(PID)
    spec, summary = turnaround.build_routes_spec(
        state, source_kind, source_id, offset, PID)
    if spec is None:
        return jsonify({"ok": False, "error": "来源不存在"}), 404
    if not spec:
        return jsonify({"ok": False,
                        "error": "来源中没有可生成养护路线的实际穿用副本"}), 400
    name = (data.get("name") or f"场间复位·{src_name}").strip()
    tid = db.create_turnaround(name, source_kind, source_id, offset, spec, summary, PID)
    SELECTED_TURNAROUND["id"] = tid
    return jsonify({"ok": True, "id": tid, "detail": _turnaround_detail(tid)})


def _turnaround_detail(tid):
    return turnaround.detail(db.load_state(PID), db.get_turnaround(tid))


@app.get("/api/turnarounds/<int:tid>")
def api_turnaround_get(tid):
    tr, err = _tr_detail_or_404(tid)
    if err:
        return err
    SELECTED_TURNAROUND["id"] = tid
    return jsonify({"ok": True, "detail": turnaround.detail(db.load_state(PID), tr)})


@app.post("/api/turnarounds/select")
def api_turnaround_select():
    tid = int((request.get_json(force=True) or {}).get("id") or 0)
    if tid:
        tr, err = _tr_detail_or_404(tid)
        if err:
            return err
    SELECTED_TURNAROUND["id"] = tid or None
    return jsonify(full_state())


@app.post("/api/care_steps/<int:sid>")
def api_care_step_update(sid):
    """拖排/改派工序：设备工位、人员、人工时刻、用时、备注。
    完工锁定工序的时间/资源不得改动；改派只钉住本工序，后续自动重算。"""
    s = db.get_care_step(sid)
    if not s or s["production_id"] != PID:
        return jsonify({"ok": False, "error": "工序不存在"}), 404
    tr = db.get_turnaround(s["turnaround_id"])
    if tr["status"] == "archived":
        return jsonify({"ok": False, "error": "工作区已归档，不能修改"}), 409
    data = request.get_json(force=True)
    sets = {}
    for k in ("resource_id", "dresser_id", "start_sec", "dur_sec", "note"):
        if k in data:
            sets[k] = data[k]
    if "dur_sec" in sets:
        try:
            sets["dur_sec"] = max(1, int(sets["dur_sec"]))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "用时必须是正整数秒"}), 400
    # 已锁（完工）工序：时刻与资源不可动，只允许改备注
    eff = turnaround.effective_care_events(db.care_events(tr["id"]))
    done = eff.get(sid, {}).get("done") or s["locked"]
    locked_fields = ("resource_id", "dresser_id", "start_sec", "dur_sec")
    if done and any(k in sets for k in locked_fields):
        return jsonify({"ok": False,
                        "error": "完工工序已锁定，不能再挪动或改派"}), 409
    # 资源类型必须与工序匹配；人员须为该剧目服装师
    con = db.connect()
    try:
        rid = sets.get("resource_id", s["resource_id"])
        if rid:
            row = con.execute("SELECT kind FROM care_resources WHERE id=? AND production_id=?",
                              (rid, PID)).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "设备/工位不存在"}), 404
            if row["kind"] != s["kind"]:
                return jsonify({"ok": False,
                                "error": f"该工序只能指派{turnaround.STEP_CN[s['kind']]}类资源"}), 400
        did = sets.get("dresser_id", s["dresser_id"])
        if did and not con.execute("SELECT 1 FROM dressers WHERE id=? AND production_id=?",
                                   (did, PID)).fetchone():
            return jsonify({"ok": False, "error": "服装师不存在"}), 400
    finally:
        con.close()
    db.update_care_step(sid, sets)
    return jsonify({"ok": True, "detail": _turnaround_detail(tr["id"])})


@app.post("/api/care_steps/<int:sid>/events")
def api_care_step_event(sid):
    """追加执行事件：start 开工 / done 完工（锁定）/ return 退回重做 / scrap 报废。
    报废使整条路线后续工序取消；return 必须留理由。"""
    s = db.get_care_step(sid)
    if not s or s["production_id"] != PID:
        return jsonify({"ok": False, "error": "工序不存在"}), 404
    tr = db.get_turnaround(s["turnaround_id"])
    if tr["status"] == "archived":
        return jsonify({"ok": False, "error": "工作区已归档，不能再登记事件"}), 409
    data = request.get_json(force=True)
    kind = data.get("kind")
    if kind not in turnaround.EVENT_KINDS:
        return jsonify({"ok": False, "error": "事件类型必须是 start/done/return/scrap"}), 400
    try:
        at_sec = int(data.get("at_sec"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "时刻必须是整数秒"}), 400
    reason = (data.get("reason") or "").strip()
    if kind in ("return", "scrap") and not reason:
        return jsonify({"ok": False,
                        "error": "退回重做/报废必须填写理由（随档保留）"}), 400
    eff = turnaround.effective_care_events(db.care_events(tr["id"]))
    evs = eff.get(sid, {})
    if evs.get("scrap"):
        return jsonify({"ok": False, "error": "该副本已报废，工序不再接受事件"}), 409
    if kind == "done":
        st = evs.get("start")
        if st and at_sec < st["at_sec"]:
            return jsonify({"ok": False,
                            "error": f"完工时刻 {scheduler.fmt(at_sec)} 早于开工 "
                                     f"{scheduler.fmt(st['at_sec'])}"}), 400
    db.add_care_event(tr["id"], sid, kind, at_sec, reason, PID)
    sets = {}
    if kind == "done":
        # 完工锁定：钉在实测时段（有开工打点取开工，否则按完工-用时倒推），
        # 之后重排不再挪动
        sets["locked"] = 1
        st = eff.get("start")
        if s["start_sec"] is None:
            sets["start_sec"] = st["at_sec"] if st else max(0, at_sec - s["dur_sec"])
    elif kind == "start" and s["start_sec"] is None:
        sets["start_sec"] = at_sec
    elif kind == "return":
        # 退回重做：该工序回到待排（解锁、清人工时刻），后序随重算
        sets["locked"] = 0
        sets["start_sec"] = None
    elif kind == "scrap":
        sets["locked"] = 0
    if sets:
        db.update_care_step(sid, sets)
    return jsonify({"ok": True, "detail": _turnaround_detail(tr["id"])})


@app.post("/api/turnarounds/<int:tid>/archive")
def api_turnaround_archive(tid):
    """归档：随档保存来源记录、人工计划与执行事件。归档后只读。"""
    tr, err = _tr_detail_or_404(tid)
    if err:
        return err
    if tr["status"] == "archived":
        return jsonify({"ok": False, "error": "工作区已归档"}), 409
    state = db.load_state(PID)
    db.archive_turnaround(tid, turnaround.archive_pack(state, tr))
    return jsonify({"ok": True, "detail": _turnaround_detail(tid)})


@app.get("/export/turnarounds/<int:tid>/record")
def export_turnaround_record(tid):
    """场间复位记录（HTML，可打印）：来源、逐副本路线、执行事件与卡点。"""
    tr, err = _tr_detail_or_404(tid)
    if err:
        return "复位工作区不存在", 404
    d = turnaround.detail(db.load_state(PID), tr)
    resources = {r["id"]: r for r in db.care_resources(PID)}
    dressers = {x["id"]: x for x in db.load_state(PID)["dressers"]}
    routes = {r["id"]: r for r in d["routes"]}
    steps_by_route = {}
    for s in d["steps"]:
        steps_by_route.setdefault(s["route_id"], []).append(s)
    ev_by_step = {}
    for e in d["events"]:
        ev_by_step.setdefault(e["step_id"], []).append(e)

    def rname(rid):
        return resources.get(rid, {}).get("name", "—") if rid else "—"

    def dname(did):
        return dressers.get(did, {}).get("name", "—") if did else "—"

    rows = []
    for r in sorted(d["routes"], key=lambda x: (x["sort_key"], x["id"])):
        dl = "晚场不再引用" if r["deadline_sec"] is None else scheduler.fmt(r["deadline_sec"])
        rows.append(
            f"<tr class=route><td colspan=8><b>#{r['id']} {_xml(r['item_name'])}"
            f"第{r['copy_no']}件</b>｜收回 {scheduler.fmt(r['released_at'])}｜"
            f"就位期限 {dl}｜状态 {_xml(r['status'])}"
            + (f"｜<b class='{'ok' if r['on_time'] else 'bad'}'>"
               f"{'按时就位 ✓' if r['on_time'] else '无法按时归位 ✗'}</b>"
               if r["on_time"] is not None else "") + "</td></tr>")
        for s in sorted(steps_by_route.get(r["id"], []), key=lambda x: x["seq"]):
            win = (f"{scheduler.fmt(s['start'])}–{scheduler.fmt(s['end'])}"
                   if s.get("start") is not None else "未排")
            ev_txt = "；".join(
                f"{ {'start':'开工','done':'完工','return':'退回重做','scrap':'报废'}[e['kind']]}"
                f"@{scheduler.fmt(e['at_sec'])}"
                + (f"：{_xml(e['reason'])}" if e["reason"] else "")
                for e in ev_by_step.get(s["id"], [])) or "—"
            rows.append(
                f"<tr><td></td><td>{s['seq']} {s['label']}{'🔒' if s['locked'] else ''}</td>"
                f"<td>{s['dur_sec']}s</td><td>{rname(s.get('res_resource_id'))}</td>"
                f"<td>{dname(s.get('res_dresser_id'))}</td><td>{win}</td>"
                f"<td>{_xml(s.get('note') or '')}</td><td class=note>{ev_txt}</td></tr>")
    src = d["source"]
    if src.get("kind") == "run":
        src_txt = f"连排#{src.get('run_id')} {_xml(src.get('run_name',''))}" \
                  f"（基准修订#{src.get('revision_id')}）实际穿用记录"
    else:
        src_txt = f"已确认方案：修订#{src.get('revision_id')} {_xml(src.get('note',''))}"
    conf = "".join(
        f"<li>[{scheduler.fmt(c['time'])}] {_xml(c['message'])}</li>"
        for c in d["conflicts"]) or "<li>无 ✓</li>"
    html = f"""<!doctype html><html lang=zh><meta charset=utf-8>
<title>场间复位记录 · {_xml(tr['name'])}</title>
<style>body{{font-family:sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%;margin:8px 0}}
td,th{{border:1px solid #999;padding:5px 7px;font-size:12px;vertical-align:top}}
tr.route td{{background:#eef3fa}}h1{{font-size:19px}}h2{{font-size:15px;margin-top:18px}}
.note{{color:#a04000}}.bad{{color:#c0392b}}.ok{{color:#27ae60}}</style>
<h1>场间复位记录 · {_xml(tr['name'])}</h1>
<p>来源：{src_txt}｜晚场偏移 {tr['evening_offset_sec']}s｜状态：
{'已归档（只读）' if tr['status'] == 'archived' else '进行中'}</p>
<h2>逐副本养护路线（人工计划 + 执行事件）</h2>
<table><tr><th></th><th>工序</th><th>用时</th><th>设备/工位</th><th>人员</th>
<th>计划时段</th><th>备注</th><th>执行事件</th></tr>{''.join(rows)}</table>
<h2>卡点与提醒</h2><ul>{conf}</ul></html>"""
    return html


# ---------------- 养护资源（设备/工位） ----------------

CARE_RESOURCE_KINDS = {"clean", "dry", "mend", "press", "load"}


@app.post("/api/care_resources")
def api_care_resource_create():
    data = request.get_json(force=True)
    kind = data.get("kind")
    if kind not in CARE_RESOURCE_KINDS:
        return jsonify({"ok": False, "error": "类型必须是 clean/dry/mend/press/load"}), 400
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "名称必填"}), 400
    is_station = 1 if kind in turnaround.STATION_KINDS else 0
    rid = db.upsert_care_resource(
        PID, name, kind, is_station,
        max(1, int(data.get("capacity") or 1)),
        max(0, int(data.get("cool_down_sec") or 0)),
        data.get("skill_id") or None)
    return jsonify({"ok": True, "id": rid})


@app.post("/api/care_resources/<int:rid>")
def api_care_resource_update(rid):
    data = request.get_json(force=True)
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM care_resources WHERE id=? AND production_id=?",
                          (rid, PID)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "资源不存在"}), 404
        kind = data.get("kind") or row["kind"]
        if kind not in CARE_RESOURCE_KINDS:
            return jsonify({"ok": False, "error": "类型无效"}), 400
        db.upsert_care_resource(
            PID, (data.get("name") or row["name"]).strip(), kind,
            1 if kind in turnaround.STATION_KINDS else 0,
            max(1, int(data.get("capacity", row["capacity"]))),
            max(0, int(data.get("cool_down_sec", row["cool_down_sec"]))),
            data.get("skill_id", row["skill_id"]) or None, rid=rid)
    finally:
        con.close()
    return jsonify(full_state())


@app.delete("/api/care_resources/<int:rid>")
def api_care_resource_delete(rid):
    db.delete_care_resource(PID, rid)
    return jsonify(full_state())


# ---------------- 导出 ----------------

def _task_detail(state, sched, tid):
    tasks = {t["id"]: t for t in state["tasks"]}
    actors = {a["id"]: a for a in state["actors"]}
    scenes = {s["id"]: s for s in state["scenes"]}
    pos = {p["id"]: p for p in state["positions"]}
    drs = {d["id"]: d for d in state["dressers"]}
    t = tasks[tid]
    w = sched["windows"][tid]
    return {
        "task": t, "actor": actors.get(t["actor_id"]),
        "from_scene": scenes.get(t["from_scene_id"]), "to_scene": scenes.get(t["to_scene_id"]),
        "position": pos.get(t["position_id"]), "dresser": drs.get(t["dresser_id"]),
        "window": w,
        "actions": [a for a in sched["actions"] if a["task_id"] == tid],
    }


@app.get("/export/cue/<int:actor_id>")
def export_cue(actor_id):
    """个人换装提示单（HTML，可打印）。"""
    state = db.load_state(PID)
    sched = scheduler.compute_schedule(state)
    actor = next((a for a in state["actors"] if a["id"] == actor_id), None)
    if not actor:
        return "演员不存在", 404
    details = [_task_detail(state, sched, t["id"]) for t in state["tasks"]
               if t["actor_id"] == actor_id]
    details.sort(key=lambda d: d["window"]["start"])
    rows = []
    def _staff_names(tid, a):
        st = a.get("staff", {})
        ids = st.get("ids") if st else None
        if ids is None and t["dresser_id"]:
            ids = [t["dresser_id"]]
        names = [drs[i]["name"] for i in (ids or []) if i in drs]
        return "、".join(names) if names else ""

    for d in details:
        parts = []
        for a in d["actions"]:
            who = _staff_names(d["task"], a)
            parts.append(f"{a['label']}({a['dur']}s" + (f"·{who}" if who else "") + ")")
        acts = " → ".join(parts)
        rows.append(
            f"<tr><td>{scheduler.fmt(d['window']['start'])}</td>"
            f"<td>{d['from_scene']['name']} → {d['to_scene']['name']}</td>"
            f"<td>{d['position']['name'] if d['position'] else '-'}</td>"
            f"<td>{d['dresser']['name'] if d['dresser'] else '自助'}</td>"
            f"<td>{scheduler.fmt(d['window']['deadline'])}</td>"
            f"<td class='acts'>{acts}</td></tr>")
    html = f"""<!doctype html><html lang=zh><meta charset=utf-8>
<title>换装提示单 · {actor['name']}</title>
<style>body{{font-family:sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #999;padding:6px 8px;font-size:13px;vertical-align:top}}
h1{{font-size:20px}}</style>
<h1>个人换装提示单 · {actor['name']}（{actor['code']}）</h1>
<table><tr><th>开始</th><th>场次</th><th>换装位</th><th>服装师</th><th>须于前完成</th><th>动作顺序</th></tr>
{''.join(rows)}</table>"""
    return html


@app.get("/export/dresser_cue/<int:did>")
def export_dresser_cue(did):
    """服装师个人提示单：只列其参与的动作；?run_id= 时引用连排冻结分工。"""
    run_id = request.args.get("run_id", type=int)
    if run_id:
        d = _run_detail(run_id)
        if not d:
            return "连排不存在", 404
        plan = d["plan"]
        dressers = {x["id"]: x for x in plan["dressers"]}
        actors = {x["id"]: x for x in plan["actors"]}
        scenes = {x["id"]: x for x in plan["scenes"]}
        tasks = {x["id"]: x for x in plan["tasks"]}
        actions = [a for a in plan["actions"] if did in (a.get("staff_ids") or [])]
        title_extra = f"｜{d['run']['name']}（冻结分工，基准修订#{d['run']['revision_id']}）"
    else:
        state = db.load_state(PID)
        sched = scheduler.compute_schedule(state)
        dressers = {x["id"]: x for x in state["dressers"]}
        actors = {x["id"]: x for x in state["actors"]}
        scenes = {x["id"]: x for x in state["scenes"]}
        tasks = {x["id"]: x for x in state["tasks"]}
        actions = [a for a in sched["actions"]
                   if did in (a.get("staff", {}) or {}).get("ids", [])]
        title_extra = ""
    dresser = dressers.get(did)
    if not dresser:
        return "服装师不存在", 404
    rows = []
    for a in sorted(actions, key=lambda x: x["start"]):
        t = tasks[a["task_id"]]
        ac = actors.get(t["actor_id"])
        fs = scenes.get(t["from_scene_id"])
        ts = scenes.get(t["to_scene_id"])
        mates = []
        if run_id:
            ids = [x for x in (a.get("staff_ids") or []) if x != did]
        else:
            ids = [x for x in a["staff"]["ids"] if x != did]
        mates = "、".join(dressers[i]["name"] for i in ids if i in dressers)
        lead = ((a.get("lead_id") if run_id else a["staff"].get("lead_id")) == did)
        rows.append(
            f"<tr><td>{scheduler.fmt(a['start'])}</td>"
            f"<td>#{t['id']} {ac['name'] if ac else ''}</td>"
            f"<td>{fs['name'] if fs else ''} → {ts['name'] if ts else ''}</td>"
            f"<td>{a['label']}</td>"
            f"<td>{'负责人' if lead else '协作'}</td>"
            f"<td>{mates or '—'}</td>"
            f"<td>{'🔒' if (a.get('staff_locked') if run_id else a['staff'].get('locked')) else ''}</td></tr>")
    html = f"""<!doctype html><html lang=zh><meta charset=utf-8>
<title>服装师提示单 · {dresser['name']}</title>
<style>body{{font-family:sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #999;padding:6px 8px;font-size:13px;vertical-align:top}}
h1{{font-size:20px}}</style>
<h1>服装师个人提示单 · {dresser['name']}{title_extra}</h1>
<table><tr><th>开始</th><th>演员/任务</th><th>场次</th><th>负责动作</th><th>角色</th>
<th>搭档（双人同步/交接）</th><th>锁定</th></tr>
{''.join(rows)}</table>"""
    return html


@app.get("/export/flow.csv")
def export_flow():
    """服装流转表（CSV）：每件服装在哪个任务被脱下/穿上、所在服装车、状态。"""
    state = db.load_state(PID)
    sched = scheduler.compute_schedule(state)
    items = {i["id"]: i for i in state["items"]}
    carts = {c["id"]: c for c in state["carts"]}
    tasks = {t["id"]: t for t in state["tasks"]}
    actors = {a["id"]: a for a in state["actors"]}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时刻", "动作", "服装/道具", "演员", "任务", "服装车", "状态"])
    for a in sched["actions"]:
        if a.get("item_id") is None:
            continue
        it = items[a["item_id"]]
        t = tasks[a["task_id"]]
        cart = carts.get(it["cart_id"])
        w.writerow([scheduler.fmt(a["start"]), a["label"], it["name"],
                    actors[t["actor_id"]]["name"], f"#{t['id']}",
                    cart["name"] if cart else "-", it["status"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=costume_flow.csv"})


@app.get("/export/timeline.svg")
def export_timeline_svg():
    """带冲突标记的 SVG 时间线。"""
    state = db.load_state(PID)
    sched = scheduler.compute_schedule(state)
    actors = {a["id"]: a for a in state["actors"]}
    tasks = {t["id"]: t for t in state["tasks"]}
    total = max((s["start_sec"] + s["duration_sec"] for s in state["scenes"]), default=600) + 60
    scale = 900.0 / max(total, 1)
    lane_h, top = 46, 30
    lanes = list(state["actors"])
    h = top + lane_h * len(lanes) + 30
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{h}" '
             f'font-family="sans-serif" font-size="11">',
             f'<rect width="960" height="{h}" fill="#fff"/>']
    # 场次刻度
    for s in state["scenes"]:
        x = 40 + s["start_sec"] * scale
        wdt = s["duration_sec"] * scale
        parts.append(f'<rect x="{x:.1f}" y="8" width="{wdt:.1f}" height="14" fill="#dde6f2"/>')
        parts.append(f'<text x="{x+2:.1f}" y="19" fill="#334">{s["name"]}</text>')
    # 任务块
    for i, a in enumerate(lanes):
        y = top + i * lane_h
        parts.append(f'<text x="2" y="{y+22}">{a["name"]}</text>')
        parts.append(f'<line x1="40" y1="{y+lane_h}" x2="950" y2="{y+lane_h}" stroke="#eee"/>')
    for tid, w in sched["windows"].items():
        t = tasks[tid]
        ai = next((i for i, a in enumerate(lanes) if a["id"] == t["actor_id"]), 0)
        y = top + ai * lane_h + 8
        x = 40 + w["start"] * scale
        wdt = max(3, (w["end"] - w["start"]) * scale)
        color = "#c0392b" if not w["ok"] else ("#7f8c8d" if t["locked"] else "#2980b9")
        parts.append(f'<rect x="{x:.1f}" y="{y}" width="{wdt:.1f}" height="26" rx="3" '
                     f'fill="{color}" opacity="0.85"/>')
        parts.append(f'<text x="{x+3:.1f}" y="{y+16}" fill="#fff">#{tid}'
                     f'{"🔒" if t["locked"] else ""}</text>')
        dx = 40 + w["deadline"] * scale
        parts.append(f'<line x1="{dx:.1f}" y1="{y-2}" x2="{dx:.1f}" y2="{y+30}" '
                     f'stroke="#c0392b" stroke-dasharray="3 2"/>')
    # 冲突标记
    for c in sched["conflicts"]:
        tid = c["task_id"]
        t = tasks.get(tid)
        if not t:
            continue
        ai = next((i for i, a in enumerate(lanes) if a["id"] == t["actor_id"]), 0)
        y = top + ai * lane_h
        x = 40 + c["time"] * scale
        color = "#e67e22" if c["type"] == "review" else "#e74c3c"
        parts.append(f'<path d="M{x:.1f},{y+2} l5,-8 l5,8 z" fill="{color}"/>')
        parts.append(f'<text x="{x+4:.1f}" y="{y}" fill="{color}" font-size="9">'
                     f'{_xml(c["message"][:28])}</text>')
    parts.append("</svg>")
    svg = "".join(parts)
    if request.args.get("dl"):
        return Response(svg, mimetype="image/svg+xml",
                        headers={"Content-Disposition": "attachment; filename=timeline.svg"})
    return Response(svg, mimetype="image/svg+xml")


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------- 连排导出 ----------------

@app.get("/export/run/<int:run_id>/compare.svg")
def export_run_compare_svg(run_id):
    """计划—实测叠放 SVG：每个演员泳道内上为计划、下为实测，标出异常与首个偏差。"""
    d = _run_detail(run_id)
    if not d:
        return "连排不存在", 404
    plan, ana = d["plan"], d["analysis"]
    tasks = {t["id"]: t for t in plan["tasks"]}
    lanes = [a for a in plan["actors"] if any(t["actor_id"] == a["id"] for t in plan["tasks"])]
    total = max((s["start_sec"] + s["duration_sec"] for s in plan["scenes"]), default=600) + 60
    scale = 900.0 / max(total, 1)
    lane_h, top = 52, 34
    h = top + lane_h * len(lanes) + 46
    lane_of = {a["id"]: i for i, a in enumerate(lanes)}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{h}" '
             f'font-family="sans-serif" font-size="11">',
             f'<rect width="960" height="{h}" fill="#fff"/>']
    for s in plan["scenes"]:
        x = 40 + s["start_sec"] * scale
        parts.append(f'<rect x="{x:.1f}" y="8" width="{s["duration_sec"]*scale:.1f}" '
                     f'height="14" fill="#dde6f2"/>')
        parts.append(f'<text x="{x+2:.1f}" y="19" fill="#334">{_xml(s["name"])}</text>')
    for a in lanes:
        y = top + lane_of[a["id"]] * lane_h
        parts.append(f'<text x="2" y="{y+26}">{_xml(a["name"])}</text>')
        parts.append(f'<line x1="40" y1="{y+lane_h}" x2="950" y2="{y+lane_h}" stroke="#eee"/>')
    ivs = ana["task_intervals"]
    for tid, w in plan["windows"].items():
        t = tasks.get(tid)
        if not t or t["actor_id"] not in lane_of:
            continue
        y = top + lane_of[t["actor_id"]] * lane_h
        x = 40 + w["start"] * scale
        wdt = max(3, (w["end"] - w["start"]) * scale)
        color = "#7f8c8d" if t["locked"] else "#2980b9"
        parts.append(f'<rect x="{x:.1f}" y="{y+4}" width="{wdt:.1f}" height="13" rx="2" '
                     f'fill="{color}" opacity="0.85"/>')
        parts.append(f'<text x="{x+2:.1f}" y="{y+14}" fill="#fff" font-size="9">#{tid}计划</text>')
        dx = 40 + w["deadline"] * scale
        parts.append(f'<line x1="{dx:.1f}" y1="{y+2}" x2="{dx:.1f}" y2="{y+36}" '
                     f'stroke="#c0392b" stroke-dasharray="3 2"/>')
        iv = ivs.get(str(tid))
        if iv:
            ax = 40 + iv[0] * scale
            aw = max(3, (iv[1] - iv[0]) * scale)
            late = iv[1] > w["end"] + rehearsal.DEVIATION_SEC
            acolor = "#c0392b" if late else "#27ae60"
            parts.append(f'<rect x="{ax:.1f}" y="{y+21}" width="{aw:.1f}" height="13" rx="2" '
                         f'fill="{acolor}" opacity="0.9"/>')
            parts.append(f'<text x="{ax+2:.1f}" y="{y+31}" fill="#fff" font-size="9">实测</text>')
        else:
            parts.append(f'<text x="{x+2:.1f}" y="{y+31}" fill="#bbb" font-size="9">未打点</text>')
    # 异常打点（含理由）与首个偏差
    for e in d["events"]:
        if e["kind"] != "exception" or e["task_id"] not in tasks:
            continue
        t = tasks[e["task_id"]]
        if t["actor_id"] not in lane_of:
            continue
        y = top + lane_of[t["actor_id"]] * lane_h
        x = 40 + e["at_sec"] * scale
        parts.append(f'<path d="M{x:.1f},{y+38} l4,-6 l4,6 z" fill="#e74c3c"/>')
        parts.append(f'<text x="{x+6:.1f}" y="{y+38}" fill="#c0392b" font-size="9">'
                     f'{_xml(e["reason"][:20])}</text>')
    fd = ana["first_deviation"]
    if fd and fd["task_id"] in tasks and tasks[fd["task_id"]]["actor_id"] in lane_of:
        t = tasks[fd["task_id"]]
        y = top + lane_of[t["actor_id"]] * lane_h
        x = 40 + fd["time"] * scale
        parts.append(f'<path d="M{x:.1f},{y-2} l5,8 l-10,0 z" fill="#e67e22"/>')
        parts.append(f'<text x="{x+6:.1f}" y="{y+2}" fill="#d35400" font-size="9">'
                     f'首个偏差 #{fd["task_id"]} {_xml(fd["label"])}</text>')
    ly = h - 30
    parts.append(f'<rect x="40" y="{ly}" width="12" height="12" fill="#2980b9"/>'
                 f'<text x="56" y="{ly+10}">计划</text>'
                 f'<rect x="100" y="{ly}" width="12" height="12" fill="#27ae60"/>'
                 f'<text x="116" y="{ly+10}">实测(按时)</text>'
                 f'<rect x="190" y="{ly}" width="12" height="12" fill="#c0392b"/>'
                 f'<text x="206" y="{ly+10}">实测(超时)</text>'
                 f'<path d="M300,{ly+12} l4,-8 l4,8 z" fill="#e74c3c"/>'
                 f'<text x="312" y="{ly+10}">异常(附理由)</text>'
                 f'<path d="M400,{ly+2} l5,8 l-10,0 z" fill="#e67e22"/>'
                 f'<text x="410" y="{ly+10}">首个偏差</text>')
    parts.append(f'<text x="40" y="{h-8}" fill="#888" font-size="10">'
                 f'{_xml(d["run"]["name"])} · 基准修订#{d["run"]["revision_id"]}</text>')
    parts.append("</svg>")
    svg = "".join(parts)
    if request.args.get("dl"):
        return Response(svg, mimetype="image/svg+xml",
                        headers={"Content-Disposition":
                                 f"attachment; filename=run{run_id}_compare.svg"})
    return Response(svg, mimetype="image/svg+xml")


@app.get("/export/run/<int:run_id>/record")
def export_run_record(run_id):
    """连排记录（HTML，可打印）：逐动作计划—实测对照，附异常与补正理由。"""
    d = _run_detail(run_id)
    if not d:
        return "连排不存在", 404
    plan, ana, run = d["plan"], d["analysis"], d["run"]
    tasks = {t["id"]: t for t in plan["tasks"]}
    actors = {a["id"]: a for a in plan["actors"]}
    dressers = {x["id"]: x for x in plan["dressers"]}
    actuals = ana["actuals"]
    by_id = {e["id"]: e for e in d["events"]}

    def dnames(ids):
        return "、".join(dressers[i]["name"] for i in ids if i in dressers) or "自助"

    def corr_note(e):
        """补正说明：原时刻 → 新时刻 + 理由。"""
        if not e["supersedes"] or e["supersedes"] not in by_id:
            return ""
        old = by_id[e["supersedes"]]
        return f"（补正 {scheduler.fmt(old['at_sec'])}→{scheduler.fmt(e['at_sec'])}：{e['reason']}）"

    KIND_CN = {"start": "开始", "done": "完成", "skip": "跳过", "exception": "异常"}
    rows = []
    for a in sorted(plan["actions"], key=lambda x: (x["task_id"], x["idx"])):
        tid = a["task_id"]
        ac = actuals.get(f"{tid}:{a['idx']}", {})
        evs = [e for e in d["events"]
               if e["task_id"] == tid and e["action_idx"] == a["idx"]]
        notes = []
        for e in evs:
            if e["kind"] == "exception":
                notes.append(f"异常@{scheduler.fmt(e['at_sec'])}：{e['reason']}")
            elif e["reason"]:
                notes.append(f"{KIND_CN[e['kind']]}{corr_note(e) or '：' + e['reason']}")
        dev = ""
        if ac.get("start") is not None:
            dd = ac["start"] - a["start"]
            dev = f"{dd:+d}s" if dd else "0"
        # 实际参与者（打点登记）对照冻结分工
        actual_ids = rehearsal._event_participants(d["events"],
                                                    rehearsal.effective_events(d["events"]),
                                                    tid, a["idx"], a)
        plan_ids = a.get("staff_ids") or []
        staff_cell = f"{dnames(actual_ids)}"
        if set(actual_ids) != set(plan_ids):
            staff_cell += f" <span style='color:#c0392b'>(计划：{dnames(plan_ids)})</span>"
        rows.append(
            f"<tr><td>#{tid}</td><td>{actors[tasks[tid]['actor_id']]['name']}</td>"
            f"<td>{a['idx']} {a['label']}</td>"
            f"<td>{scheduler.fmt(a['start'])}（{a['dur']}s）</td>"
            f"<td>{scheduler.fmt(ac['start']) if ac.get('start') is not None else ('跳过' if ac.get('skipped') else '—')}</td>"
            f"<td>{scheduler.fmt(ac['end']) if ac.get('end') is not None else '—'}</td>"
            f"<td>{staff_cell}</td>"
            f"<td>{dev}</td><td class=note>{'；'.join(notes)}</td></tr>")
    ana_rows = "".join(
        f"<li>[{scheduler.fmt(c['time'])}] 任务#{c['task_id']} {c['message']}</li>"
        for c in ana["anomalies"]) or "<li>无</li>"
    fd = ana["first_deviation"]
    fd_html = (f"<p>首个偏差：任务#{fd['task_id']} 动作{fd['action_idx']}「{fd['label']}」"
               f"@ {scheduler.fmt(fd['time'])} — {fd['message']}</p>") if fd else "<p>无显著偏差</p>"
    chain_html = "".join(f"<li>[{scheduler.fmt(c['time'])}] {c['message']}</li>"
                         for c in ana["chain"])
    import time as _time
    html = f"""<!doctype html><html lang=zh><meta charset=utf-8>
<title>连排记录 · {run['name']}</title>
<style>body{{font-family:sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #999;padding:5px 7px;font-size:12px;vertical-align:top}}
h1{{font-size:19px}}h2{{font-size:15px;margin-top:18px}}.note{{color:#a04000}}</style>
<h1>连排记录 · {run['name']}</h1>
<p>基准修订 #{run['revision_id']}｜开启 {_time.strftime('%Y-%m-%d %H:%M', _time.localtime(run['created_at']))}
｜状态 {'已结束' if run['status']=='done' else '进行中'}</p>
<h2>动作实测对照</h2>
<table><tr><th>任务</th><th>演员</th><th>动作</th><th>计划开始(时长)</th><th>实测开始</th>
<th>实测完成</th><th>实际参与者(冻结分工)</th><th>偏差</th><th>异常/补正理由</th></tr>{''.join(rows)}</table>
<h2>实测检查</h2><ul>{ana_rows}</ul>
<h2>首个偏差与等待链</h2>{fd_html}<ul>{chain_html}</ul>"""
    return html


def create_app():
    db.init_db()
    con = db.connect()
    try:
        if not con.execute("SELECT 1 FROM productions WHERE id=1").fetchone():
            con.execute("INSERT INTO productions(id,name,created_at) VALUES(1,?,?)",
                        ("示例剧目《夜航》", time.time()))
            con.commit()
    finally:
        con.close()
    return app


create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
