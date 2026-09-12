"""快速换装排程引擎。

计入：动作先后（脱外层→脱内层→穿内层→穿外层→取道具）、人员并发
（演员/服装师同一时刻只能做一个动作）、换装位容量、步行时间、
服装复用（一件服装被多人依次使用）以及清洁/维修状态。

资源模型：演员、服装师、换装位、服装副本均为「区间日历」。
任务按期望开始时刻的先后顺序排程；已锁任务保留在自己的固定时段，
只占用该时段内的资源，不会预先挤占更早任务的资源。

服装副本自穿上动作起持续占用，直到对应的脱下动作结束；
若没有脱下记录，则占用到演员穿着该件的场次结束。
"""
import math
from collections import defaultdict

WALK_SPEED = 1.3          # 米/秒（侧台快走）
PLAN_W, PLAN_H = 40, 20   # 侧台平面图尺寸（米）
EXITS = {"L": (2.0, 10.0), "R": (38.0, 10.0)}  # 左/右上场口
PROP_HANDLE_SEC = 4       # 每件道具取用耗时
CLEAN_SEC = 90            # 复用前需要的简单清洁时间（仅当服装状态为 cleaning）


def walk_sec(a, b):
    return max(1, math.ceil(math.hypot(a[0] - b[0], a[1] - b[1]) / WALK_SPEED))


def _index(lst):
    return {r["id"]: r for r in lst}


def look_items_map(state):
    """look_id -> [item,...] 按层从内到外排序。"""
    items = _index(state["items"])
    by_look = defaultdict(list)
    for li in state["look_items"]:
        it = items.get(li["item_id"])
        if it:
            by_look[li["look_id"]].append(it)
    for v in by_look.values():
        v.sort(key=lambda i: (i["layer"], i["id"]))
    return by_look


def find_look(state, actor_id, scene_id):
    for l in state["looks"]:
        if l["actor_id"] == actor_id and l["scene_id"] == scene_id:
            return l
    return None


def build_actions(task, state):
    """把一个换装任务展开为有序动作列表。"""
    scenes = _index(state["scenes"])
    positions = _index(state["positions"])
    carts = _index(state["carts"])
    by_look = look_items_map(state)

    from_sc, to_sc = scenes[task["from_scene_id"]], scenes[task["to_scene_id"]]
    pos = positions.get(task["position_id"])
    exit_pt = EXITS.get(task["exit_side"], EXITS["L"])
    pos_pt = (pos["x"], pos["y"]) if pos else exit_pt

    prev_look = find_look(state, task["actor_id"], from_sc["id"])
    next_look = find_look(state, task["actor_id"], to_sc["id"])
    prev_items = by_look.get(prev_look["id"], []) if prev_look else []
    next_items = by_look.get(next_look["id"], []) if next_look else []
    prev_ids = {i["id"] for i in prev_items}
    next_ids = {i["id"] for i in next_items}

    doff = [i for i in sorted(prev_items, key=lambda x: -x["layer"]) if i["id"] not in next_ids]
    don = [i for i in next_items if i["id"] not in prev_ids]
    props = [i for i in don if i["kind"] == "prop"]
    don_wear = [i for i in don if i["kind"] != "prop"]

    acts = [{"kind": "walk", "label": "退场→换装位", "dur": walk_sec(exit_pt, pos_pt)}]
    for it in doff:
        acts.append({"kind": "doff", "label": f"脱·{it['name']}", "dur": it["doff_sec"],
                     "item_id": it["id"], "needs_dresser": it["kind"] != "prop"})
    for it in don_wear:
        acts.append({"kind": "don", "label": f"穿·{it['name']}", "dur": it["don_sec"],
                     "item_id": it["id"], "needs_dresser": True})
    for it in props:
        cart = carts.get(it["cart_id"]) if it["cart_id"] else None
        cart_pt = (cart["x"], cart["y"]) if cart else pos_pt
        acts.append({"kind": "fetch", "label": f"取道具·{it['name']}",
                     "dur": walk_sec(pos_pt, cart_pt) * 2 + PROP_HANDLE_SEC,
                     "item_id": it["id"], "needs_dresser": False})
    acts.append({"kind": "walk", "label": "换装位→上场口", "dur": walk_sec(pos_pt, exit_pt)})

    window = {
        "ready": from_sc["start_sec"] + from_sc["duration_sec"],  # 演员退出上一场
        "deadline": to_sc["start_sec"],                            # 下一场开场
    }
    return acts, window


def _scene_end(state, scene_id):
    for s in state["scenes"]:
        if s["id"] == scene_id:
            return s["start_sec"] + s["duration_sec"]
    return 0


# ---------------- 服装穿着区间 ----------------

def _wear_segments(state, tasks):
    """(actor,item) -> [段落]：一件服装在一名演员身上连续穿着的场次区间。

    段落起点任务（don_task）= 进入首场的换装任务；没有则视为开场前已穿上（从 0 占用）。
    段落终点任务（doff_task）= 离开末场的换装任务；没有则占用到末场结束。
    """
    by_look = look_items_map(state)
    scenes = _index(state["scenes"])
    look_of = {(l["actor_id"], l["scene_id"]): l["id"] for l in state["looks"]}
    actor_scenes = defaultdict(list)
    for (aid, sid) in look_of:
        actor_scenes[aid].append(sid)
    don_task, doff_task = {}, {}
    for t in tasks:
        don_task[(t["actor_id"], t["to_scene_id"])] = t["id"]
        doff_task[(t["actor_id"], t["from_scene_id"])] = t["id"]
    segs = defaultdict(list)
    for aid, sids in actor_scenes.items():
        sids.sort(key=lambda sid: (scenes[sid]["start_sec"], scenes[sid]["seq"]))
        item_ids = set()
        for sid in sids:
            item_ids.update(i["id"] for i in by_look.get(look_of[(aid, sid)], []))
        for iid in item_ids:
            run = None
            for sid in sids:
                present = any(i["id"] == iid for i in by_look.get(look_of[(aid, sid)], []))
                if present:
                    if run is None:
                        run = {"item_id": iid, "start_scene": sid, "end_scene": sid,
                               "don_task": don_task.get((aid, sid)),
                               "doff_task": doff_task.get((aid, sid))}
                    else:
                        run["end_scene"] = sid
                        run["doff_task"] = doff_task.get((aid, sid))
                elif run is not None:
                    segs[(aid, iid)].append(run)
                    run = None
            if run is not None:
                segs[(aid, iid)].append(run)
    return segs


def _seg_release(seg, tasks_by_id, scenes, items, doff_actuals=None):
    """段落释放时刻：有脱下任务 → 该任务中脱下动作的结束时刻
    （优先取上一轮排程的实际值 doff_actuals；首轮按期望开始时刻估算）；
    没有脱下记录 → 演员穿着该件的末场结束时刻。"""
    it = items.get(seg["item_id"])
    extra = CLEAN_SEC if it and it["status"] == "cleaning" else 0
    dt = tasks_by_id.get(seg.get("doff_task"))
    if dt is not None:
        if doff_actuals and (dt["id"], seg["item_id"]) in doff_actuals:
            return doff_actuals[(dt["id"], seg["item_id"])] + extra
        cur = dt["_desired"]
        for a in dt["_acts"]:
            cur += a["dur"]
            if a["kind"] == "doff" and a.get("item_id") == seg["item_id"]:
                return cur + extra
    sc = scenes.get(seg["end_scene"])
    return (sc["start_sec"] + sc["duration_sec"]) if sc else 0


def _copy_gap_abs(cps, t, end):
    """最早 t'≥t 使某副本在 [t', end) 全程空闲；无可行副本返回 None。"""
    best = None
    for cp in cps:
        cur = t
        ok = True
        for iv in sorted(cp):
            if iv[1] <= cur:
                continue
            if iv[0] >= end:
                break
            cur = iv[1]          # 与 [cur,end) 相交 → 推到该区间结束之后
            if cur >= end:
                ok = False
                break
        if ok and (best is None or cur < best):
            best = cur
    return best


def _copy_gap_dur(cps, t, dur):
    """最早 t'≥t 使某副本在 [t', t'+dur) 空闲（仅保证穿上动作本身不重叠）。"""
    best = None
    for cp in cps:
        cur = t
        for iv in sorted(cp):
            if iv[1] <= cur:
                continue
            if iv[0] >= cur + dur:
                break
            cur = iv[1]
        if best is None or cur < best:
            best = cur
    return best if best is not None else t


def _pick_copy(cps, t, release):
    """在 t 时刻选一件副本：要求 [t, release) 不与该副本已有区间冲突。
    返回 (副本下标, 是否完全不冲突)；全部被占用返回 None。"""
    best = None
    for idx, cp in enumerate(cps):
        if any(iv[0] <= t < iv[1] for iv in cp):
            continue
        nxt = min((iv[0] for iv in cp if iv[0] > t), default=float("inf"))
        if nxt >= release:
            return (idx, True)
        if best is None or nxt > best[1]:
            best = (idx, nxt)
    return (best[0], False) if best else None


def _overlap(ivs, s, e):
    for iv in ivs:
        if iv[0] < e and iv[1] > s:
            return iv
    return None


def _capacity_shift(intervals, cap, s, e):
    """若 [s,e) 内并发数超容量，返回需要顺延的秒数。"""
    overlap = [iv for iv in intervals if iv[0] < e and iv[1] > s]
    if len(overlap) < cap:
        return 0
    return max(1, min(iv[1] for iv in overlap) - s)


# ---------------- 排程 ----------------

def compute_schedule(state, overrides=None):
    """对全部任务排程。

    overrides: {task_id: {"start_sec":..,"position_id":..,"dresser_id":..}} 临时改动，
    用于“替代排法”试算，不写库。
    返回 {actions, windows, conflicts}

    定点迭代：首轮按期望开始时刻估算副本释放点；随后每轮用上一轮的
    实际脱下结束时刻作为精确释放点重排，直到释放点收敛。资源冲突
    （如服装师争用）使脱下顺延的，副本占用随之延长到实际脱下结束，
    最终排程不会保留因估算提前释放造成的重复分配。
    """
    overrides = overrides or {}
    scenes = _index(state["scenes"])
    items = _index(state["items"])
    positions = _index(state["positions"])

    tasks = []
    for t in state["tasks"]:
        t = dict(t)
        ov = overrides.get(t["id"])
        if ov:
            t.update({k: v for k, v in ov.items() if v is not None})
        acts, win = build_actions(t, state)
        t["_acts"] = acts
        t["_win"] = win
        if t["locked"] and t["start_sec"] is not None:
            t["_desired"] = t["start_sec"]          # 锁定任务：固定时段
        elif t["start_sec"] is not None:
            t["_desired"] = max(win["ready"], t["start_sec"])
        else:
            t["_desired"] = win["ready"]
        tasks.append(t)
    # 按期望开始时刻先后处理：较早任务先占用资源；
    # 锁定任务只保留自己的固定时段，不会预先挤占更早任务的服装师/换装位。
    tasks.sort(key=lambda t: (t["_desired"], 0 if t["locked"] else 1, t["id"]))
    tasks_by_id = {t["id"]: t for t in tasks}
    segs = _wear_segments(state, tasks)

    doff_actuals = None
    result = None
    for _ in range(12):
        result = _schedule_once(state, tasks, tasks_by_id, scenes, items,
                                positions, segs, doff_actuals)
        new_actuals = {(a["task_id"], a["item_id"]): a["end"]
                       for a in result["actions"]
                       if a["kind"] == "doff" and a.get("item_id") is not None}
        converged = doff_actuals is not None and new_actuals == doff_actuals
        if converged and not _find_copy_overlaps(result["copies"]):
            break  # 释放点收敛且无交叠：本轮使用的即为实际脱下结束时刻
        if converged:
            break  # 收敛但仍有交叠：由返回前的修复兜底
        doff_actuals = new_actuals
    # 返回前保证：同一副本的实际占用区间不得交叠
    _repair_copy_overlaps(result, items)
    result["conflicts"].sort(key=lambda c: (c["time"], c["task_id"]))
    return result


def _find_copy_overlaps(copies):
    """列出所有同一副本上交叠的占用区间对。"""
    bad = []
    for iid, cps in copies.items():
        for ci, cp in enumerate(cps):
            ivs = sorted(cp, key=lambda x: (x[0], x[1]))
            for a, b in zip(ivs, ivs[1:]):
                if b[0] < a[1]:
                    bad.append((iid, ci, a, b))
    return bad


def _repair_copy_overlaps(result, items):
    """兜底：移除同一副本上较晚的交叠分配并记录冲突，保证返回结果无重复分配。"""
    for iid, cps in result["copies"].items():
        it = items.get(iid)
        name = it["name"] if it else f"#{iid}"
        for cp in cps:
            kept = []
            for iv in sorted(cp, key=lambda x: (x[0], x[1])):
                if kept and iv[0] < kept[-1][1]:
                    tid = _iv_task_id(iv)
                    result["conflicts"].append({
                        "type": "item", "task_id": tid, "time": iv[0],
                        "message": f"缺件：{name} 副本占用交叠，已取消较晚的重复分配"})
                    continue
                kept.append(iv)
            cp[:] = kept


def _iv_task_id(iv):
    try:
        return int(str(iv[2]).split(":")[1]) if str(iv[2]).startswith("task:") else 0
    except (IndexError, ValueError):
        return 0


def _schedule_once(state, tasks, tasks_by_id, scenes, items, positions, segs,
                   doff_actuals):
    """单轮排程。doff_actuals: {(task_id,item_id): 实际脱下结束时刻}（上一轮结果）。"""

    actor_busy = defaultdict(list)     # actor_id -> [(s,e,task_id)]
    dresser_busy = defaultdict(list)   # dresser_id -> [(s,e,task_id)]
    pos_busy = defaultdict(list)       # position_id -> [(s,e,task_id)]

    # 服装副本日历：清洁/维修不可用区间 + 开场前已穿着的区间
    copies = {}
    for it in state["items"]:
        cps = [[] for _ in range(max(1, it["copies"]))]
        if it["status"] != "ok" and it["available_at"] > 0:
            for cp in cps:
                cp.append([0, it["available_at"], "status"])
        copies[it["id"]] = cps
    segs = segs if segs is not None else _wear_segments(state, tasks)
    actions, windows, conflicts = [], {}, []

    # 每个穿上动作在本轮的释放点（随定点迭代更新为实际值）
    for t in tasks:
        for a in t["_acts"]:
            if a["kind"] == "don" and a.get("item_id") is not None:
                seg = next((g for g in segs.get((t["actor_id"], a["item_id"]), [])
                            if g["start_scene"] == t["to_scene_id"]), None)
                a["_release"] = _seg_release(seg, tasks_by_id, scenes, items,
                                             doff_actuals) if seg \
                    else _scene_end(state, t["to_scene_id"])

    worn = {}                          # (actor,item) -> (副本下标, 区间)
    for (aid, iid), lst in segs.items():
        for seg in lst:
            if seg["don_task"] is None:    # 无穿上任务：从穿着首场的开场起占用
                rel = _seg_release(seg, tasks_by_id, scenes, items, doff_actuals)
                sc = scenes.get(seg["start_scene"])
                start0 = sc["start_sec"] if sc else 0
                pick = _pick_copy(copies[iid], start0, rel)
                if pick is None or not pick[1]:
                    it = items.get(iid)
                    conflicts.append({
                        "type": "item", "task_id": 0, "time": start0,
                        "message": f"缺件：{it['name'] if it else iid} 开场穿着无可用副本（副本不足）"})
                    continue
                idx = pick[0]
                iv = [start0, rel, f"init:a{aid}"]
                copies[iid][idx].append(iv)
                worn[(aid, iid)] = (idx, iv)

    for t in tasks:
        tid = t["id"]
        aid = t["actor_id"]
        win = t["_win"]
        s = t["_desired"]
        delay_notes = []
        item_wait_notes = []
        laid = None
        for _ in range(80):
            # 1) 顺序布局动作；穿上动作须在 [开始, 释放点) 全程有可用副本
            item_wait_notes = []
            cur = s
            laid = []
            for a in t["_acts"]:
                a = dict(a)
                a["start"] = cur
                if a["kind"] == "don" and a.get("item_id") is not None:
                    iid = a["item_id"]
                    it = items.get(iid)
                    rel = max(a.get("_release", 0), cur + a["dur"])
                    t1 = _copy_gap_abs(copies[iid], cur, rel)
                    if t1 is None:
                        # 穿着全程无可行副本：退化为仅保证穿上动作本身不重叠，
                        # 提交阶段若仍无法全程分配则拒绝（不产生重复分配）
                        t2 = _copy_gap_dur(copies[iid], cur, a["dur"])
                        item_wait_notes.append({
                            "type": "item", "task_id": tid, "time": t2,
                            "message": f"缺件：{a['label']} 穿着期间无连续可用副本"})
                        a["start"] = t2
                    elif t1 > cur:
                        why = "清洁/维修中" if it and it["status"] != "ok" \
                            else "复用等待（前一位演员尚未脱下）"
                        item_wait_notes.append({
                            "type": "item", "task_id": tid, "time": t1,
                            "message": f"缺件/复用冲突：{a['label']} 需等到 "
                                       f"{fmt(t1)}（{why}）"})
                        a["start"] = t1
                a["end"] = a["start"] + a["dur"]
                cur = a["end"]
                laid.append(a)
            start, end = laid[0]["start"], laid[-1]["end"]
            occ_s, occ_e = laid[0]["end"], laid[-1]["start"]
            if t["locked"]:
                # 锁定任务不移动：与其它占用的重叠只记录为冲突
                blk = _overlap(actor_busy[aid], start, end)
                if blk:
                    delay_notes.append({"type": "actor", "task_id": tid, "time": start,
                                        "message": f"锁定任务与任务#{blk[2]}的演员时间重叠"})
                if t["dresser_id"]:
                    blk = _overlap(dresser_busy[t["dresser_id"]], start, end)
                    if blk:
                        delay_notes.append({"type": "dresser", "task_id": tid, "time": start,
                                            "message": f"锁定任务与任务#{blk[2]}争用服装师"})
                break
            blk = _overlap(actor_busy[aid], start, end)
            if blk:
                s = max(s + 1, blk[1])
                continue
            if t["dresser_id"]:
                blk = _overlap(dresser_busy[t["dresser_id"]], start, end)
                if blk:
                    delay_notes.append({
                        "type": "dresser", "task_id": tid, "time": blk[1],
                        "message": f"服装师并发冲突：需等到 {fmt(blk[1])}"
                                   f"（正照看任务#{blk[2]}）"})
                    s = max(s + 1, blk[1])
                    continue
            if t["position_id"]:
                cap = positions[t["position_id"]]["capacity"] \
                    if t["position_id"] in positions else 1
                shift = _capacity_shift(pos_busy[t["position_id"]], cap, occ_s, occ_e)
                if shift:
                    delay_notes.append({"type": "position", "task_id": tid,
                                        "time": occ_s + shift,
                                        "message": f"换装位容量不足，顺延 {shift}s"})
                    s += shift
                    continue
            break

        start, end = laid[0]["start"], laid[-1]["end"]
        occ_s, occ_e = laid[0]["end"], laid[-1]["start"]

        # 2) 提交资源占用
        actor_busy[aid].append((start, end, tid))
        if t["dresser_id"]:
            dresser_busy[t["dresser_id"]].append((start, end, tid))
        if t["position_id"]:
            pos_busy[t["position_id"]].append((occ_s, occ_e, tid))
        item_commit_notes = []
        for a in laid:
            iid = a.get("item_id")
            if iid is None:
                continue
            if a["kind"] == "don":
                rel = max(a.get("_release", a["end"]), a["end"])
                pick = _pick_copy(copies[iid], a["start"], rel)
                if pick is None or not pick[1]:
                    # 无连续可用副本：拒绝分配，绝不放置交叠区间
                    item_commit_notes.append({
                        "type": "item", "task_id": tid, "time": a["start"],
                        "message": f"缺件：{a['label']} 穿着期间无连续可用副本，未分配"})
                    continue
                idx = pick[0]
                iv = [a["start"], rel, f"task:{tid}"]
                copies[iid][idx].append(iv)
                worn[(aid, iid)] = (idx, iv)
            elif a["kind"] == "doff":
                it = items.get(iid)
                extra = CLEAN_SEC if it and it["status"] == "cleaning" else 0
                key = (aid, iid)
                if key in worn:
                    _, iv = worn.pop(key)
                    iv[1] = a["end"] + extra   # 副本实际释放时刻 = 脱下动作结束(+清洁)
                # 无对应穿着记录（数据缺造型或该次穿上未分配）：不产生区间

        # 3) 截止检查：第一个超出截止时间的动作
        fail = next((a for a in laid if a["end"] > win["deadline"]), None)
        for a in laid:
            a["task_id"] = tid
        actions.extend(laid)
        windows[tid] = {"start": start, "end": end, "ready": win["ready"],
                        "deadline": win["deadline"], "ok": fail is None}
        if t["needs_review"]:
            conflicts.append({"type": "review", "task_id": tid, "time": start,
                              "message": "关联场次/道具有变更，待复核"})
        conflicts.extend(delay_notes)
        conflicts.extend(item_wait_notes)
        conflicts.extend(item_commit_notes)
        if fail:
            conflicts.append({
                "type": "late", "task_id": tid, "time": fail["start"],
                "message": f"最早无法按时完成的动作：{fail['label']}"
                           f"（预计 {fmt(fail['end'])}，截止 {fmt(win['deadline'])}）"})

    actions.sort(key=lambda a: (a["start"], a["task_id"]))
    conflicts.sort(key=lambda c: (c["time"], c["task_id"]))
    return {"actions": actions, "windows": windows, "conflicts": conflicts,
            "copies": copies}


def suggest(state, max_options=3):
    """针对冲突任务，试算改动较少的替代排法（不动已锁任务）。"""
    base = compute_schedule(state)
    base_n = len([c for c in base["conflicts"] if c["type"] != "review"])
    bad_tasks = sorted({c["task_id"] for c in base["conflicts"] if c["type"] in
                        ("late", "dresser", "position", "item")})
    if not bad_tasks:
        return []
    tasks = {t["id"]: t for t in state["tasks"]}
    suggestions = []
    for tid in bad_tasks:
        t = tasks[tid]
        if t["locked"]:
            continue
        best = None
        for pos in state["positions"]:
            for dr in ([None] + [d["id"] for d in state["dressers"]]):
                for shift in (0, -15, -30, 15, 30):
                    ov = {tid: {"position_id": pos["id"], "dresser_id": dr,
                                "start_sec": max(0, (t["start_sec"] or _scene_end(state, t["from_scene_id"])) + shift)}}
                    res = compute_schedule(state, overrides=ov)
                    n_conf = len([c for c in res["conflicts"] if c["type"] != "review"])
                    task_conf = len([c for c in res["conflicts"]
                                     if c["task_id"] == tid and c["type"] != "review"])
                    changes = int(pos["id"] != t["position_id"]) + int(dr != t["dresser_id"]) + int(shift != 0)
                    score = (n_conf, changes)
                    if best is None or score < best[0]:
                        best = (score, {"task_id": tid,
                                        "changes": {"position_id": pos["id"], "dresser_id": dr,
                                                    "start_sec": ov[tid]["start_sec"]},
                                        "remaining_conflicts": task_conf})
            if best and best[0][0] == 0 and best[0][1] <= 1:
                break
        if best and best[0][0] < base_n:
            suggestions.append(best[1])
        if len(suggestions) >= max_options:
            break
    return suggestions


def fmt(sec):
    sec = int(sec)
    return f"{sec//60:02d}:{sec % 60:02d}"
