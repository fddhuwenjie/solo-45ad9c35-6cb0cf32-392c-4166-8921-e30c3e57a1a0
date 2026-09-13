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

app = Flask(__name__)
PID = 1

# 允许录入/修改的字段白名单
FIELDS = {
    "scenes": ["seq", "name", "start_sec", "duration_sec"],
    "actors": ["name", "code", "default_side"],
    "looks": ["actor_id", "scene_id", "name"],
    "items": ["name", "kind", "layer", "don_sec", "doff_sec", "status",
              "available_at", "cart_id", "copies", "skill_id"],
    "dressers": ["name"],
    "skills": ["name"],
    "positions": ["name", "side", "x", "y", "capacity"],
    "carts": ["name", "side", "x", "y", "capacity"],
    "dresser_unavailable": ["dresser_id", "start_sec", "end_sec", "reason"],
    "tasks": ["actor_id", "from_scene_id", "to_scene_id", "exit_side",
              "position_id", "dresser_id", "start_sec", "locked", "note"],
}


def full_state():
    state = db.load_state(PID)
    sched = scheduler.compute_schedule(state)
    state["schedule"] = sched
    state["runs"] = db.list_runs(PID)
    return state


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
        # 仅标记该服装师实际参与的具体动作（不整任务标红）
        if skill_changed or side_changed:
            tids = [r["task_id"] for r in con.execute(
                "SELECT DISTINCT task_id FROM action_staff WHERE dresser_id=?",
                (did,)).fetchall()]
            _mark_action_review(con, tids,
                                "服装师技能变更" if skill_changed else "可支援侧台变更")
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
        con.commit()
        new_id = cur.lastrowid
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
