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
import scheduler

app = Flask(__name__)
PID = 1

# 允许录入/修改的字段白名单
FIELDS = {
    "scenes": ["seq", "name", "start_sec", "duration_sec"],
    "actors": ["name", "code", "default_side"],
    "looks": ["actor_id", "scene_id", "name"],
    "items": ["name", "kind", "layer", "don_sec", "doff_sec", "status",
              "available_at", "cart_id", "copies"],
    "dressers": ["name"],
    "positions": ["name", "side", "x", "y", "capacity"],
    "carts": ["name", "side", "x", "y", "capacity"],
    "tasks": ["actor_id", "from_scene_id", "to_scene_id", "exit_side",
              "position_id", "dresser_id", "start_sec", "locked", "note"],
}


def full_state():
    state = db.load_state(PID)
    sched = scheduler.compute_schedule(state)
    state["schedule"] = sched
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
    sets, args = [], []
    for k in ("start_sec", "position_id", "dresser_id", "locked", "note",
              "exit_side", "from_scene_id", "to_scene_id"):
        if k in data:
            sets.append(f"{k}=?")
            args.append(data[k])
    if not sets:
        return jsonify({"ok": False}), 400
    args.append(tid)
    con = db.connect()
    try:
        con.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", args)
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.post("/api/tasks/<int:tid>/clear_review")
def api_clear_review(tid):
    con = db.connect()
    try:
        con.execute("UPDATE tasks SET needs_review=0 WHERE id=?", (tid,))
        con.commit()
    finally:
        con.close()
    return jsonify(full_state())


@app.post("/api/apply_suggestion")
def api_apply_suggestion():
    data = request.get_json(force=True)
    tid = int(data["task_id"])
    ch = data["changes"]
    con = db.connect()
    try:
        cur = con.execute("SELECT locked FROM tasks WHERE id=?", (tid,)).fetchone()
        if cur and cur["locked"]:
            return jsonify({"ok": False, "error": "已锁节点不得移动"}), 409
        con.execute("UPDATE tasks SET position_id=?, dresser_id=?, start_sec=? WHERE id=?",
                    (ch.get("position_id"), ch.get("dresser_id"), ch.get("start_sec"), tid))
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
        # 场次时间或道具/服装状态变化 → 仅关联任务置为待复核
        if entity == "scenes" and {"start_sec", "duration_sec"} & set(data):
            con.execute(
                "UPDATE tasks SET needs_review=1 WHERE from_scene_id=? OR to_scene_id=?",
                (rid, rid))
        if entity == "items" and {"status", "available_at", "cart_id"} & set(data):
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
    for d in details:
        acts = " → ".join(f"{a['label']}({a['dur']}s)" for a in d["actions"])
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
